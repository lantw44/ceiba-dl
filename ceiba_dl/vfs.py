# License: LGPL3+

from collections import OrderedDict
from io import BytesIO, StringIO
from lxml import etree
from pathlib import PurePosixPath
from urllib.parse import urlencode, urlsplit, parse_qs, quote, unquote
import ast
import csv
import json
import html
import logging

# 提供給外部使用的 VFS 界面

class VFS:
    def __init__(self, request, strings, edit):
        self.logger = logging.getLogger(__name__)
        self.request = request
        self.strings = strings
        self.root = RootDirectory(self)
        self._edit = edit

    def open(self, path, cwd=None, edit_check=True, allow_students=True):
        if edit_check and hasattr(self, '_edit'):
            self._do_edit()
        if cwd == None:
            work = self.root
        else:
            work = cwd
        path = PurePosixPath(path)
        for item in path.parts:
            if item.find('/') >= 0:
                continue
            if not allow_students and work is self.root.students:
                return False
            work = work.access(item)
        if not work.ready:
            work.fetch()
        return work

    def is_root(self, node):
        return node is self.root

    def is_regular(self, node):
        return isinstance(node, Regular)

    def is_directory(self, node):
        return isinstance(node, Directory)

    def is_internal_link(self, node):
        return isinstance(node, InternalLink)

    def is_external_link(self, node):
        return isinstance(node, ExternalLink)

    def _do_edit(self):
        s = self.strings

        # add_courses
        for semester, sn in self._edit['add_courses']:
            path = PurePosixPath('/', s['dir_root_courses'], semester)
            node = self.open(path, edit_check=False)
            course = WebCourseDirectory(self, node, semester, sn)
            course.fetch()
            assert course.ready == True
            node.add(course.name, course)
            node.add(sn, InternalLink(self, node, course.name))

        # delete_files
        for path in self._edit['delete_files']:
            node = self.open(path, edit_check=False, allow_students=False)
            if node:
                if node is self.root:
                    raise ValueError('不可以刪除根資料夾')
                node.parent.unlink(PurePosixPath(path).name)
            else:
                self.root.students.queue_deletion_request(path)

        # 完成。下次 open 不會再進來了
        del self._edit


# 把 JSON 裡的多行字串轉成陣列

class OrderedDictWithLineBreak(OrderedDict):
    def __setitem__(self, key, value, **kwargs):
        if isinstance(value, str) and value.find('\n') >= 0:
            value = ['（多行字串）'] + value.replace('\r', '').split('\n')
        super().__setitem__(key, value, **kwargs)

# 產生普通資料檔的檔案名稱

def format_dirname(sn, title):
    return '{:08} {}'.format(int(sn), title.strip().rstrip('.'))

def format_filename(sn, title, extension):
    return '{}.{}'.format(format_dirname(sn, title), extension)

# 把網址轉成 ceiba_dl.Request 可以接受的格式
# 有些時候網址會有問號，但 CEIBA 不會自己跳脫，這時候可以設定 no_query_string

def url_to_path_and_args(url, no_query_string=False):
    if no_query_string:
        url = url.replace('?', '%3F')
    components = urlsplit(url)
    path = components.path
    if no_query_string:
        path = unquote(path)
        # 沒中文字的時候 CEIBA 用 %3F 代表問號（跳脫一次）
        # 有中文字的時候 CEIBA 用 %253F 代表問號（跳脫兩次）
        # 注意 ceiba_dl.Request 本身就會跳脫一次，所以這裡至多只會跳脫一次
        quote_test = path.replace('?', '').replace(' ', '')
        if quote(quote_test) != quote_test:
            path = path.replace('?', '%3F')
        args = {}
    else:
        query_string = components.query
        args = parse_qs(query_string, keep_blank_values=True)
        for key, value in args.items():
            if isinstance(value, list):
                assert len(value) == 1
                args[key] = value[0]
    return (path, args)

# lxml 遇到沒有文字時回傳 None，但空字串比較好操作

def element_get_text(element):
    return '' if element.text == None else html.unescape(element.text)

# 從雙欄表格中取得資料的輔助工具

def row_get_value(row, expected_keys, value_mappings,
    free_form=False, return_object=False, is_teacher_page=False):

    assert len(row) == 2
    assert isinstance(expected_keys, list)
    if is_teacher_page:
        assert row[0].tag == 'td'
    else:
        assert row[0].tag == 'th'
    assert row[1].tag == 'td'
    if is_teacher_page and len(row[0]) > 0:
        assert len(row[0]) == 1
        assert row[0][0].tag == 'font'
        assert ''.join(row[0].itertext()) in expected_keys
    else:
        assert row[0].text in expected_keys
    for source, mapped in value_mappings.items():
        assert isinstance(source, tuple)
        assert not is_teacher_page
        if row[1].text in source:
            return mapped
    if free_form:
        if return_object:
            return row[1]
        elif is_teacher_page:
            if len(row[1]) > 0:
                assert len(row[1]) == 1
                assert row[1][0].tag == 'font'
                return list(row[1].itertext())
            else:
                return [ element_get_text(row[1]) ]
        else:
            return element_get_text(row[1])
    else:
        assert False

# 有些網頁連結是用 JavaScript 的 window.open 做的
def js_window_open_get_url(js_script):
    # 借用 Python parser 來讀 JavaScript code
    python_ast = ast.parse(js_script)
    assert len(python_ast.body) == 1
    assert type(python_ast.body[0]) == ast.Expr
    assert type(python_ast.body[0].value) == ast.Call
    assert python_ast.body[0].value.func.value.id == 'window'
    assert python_ast.body[0].value.func.attr == 'open'
    assert len(python_ast.body[0].value.args) == 3
    return python_ast.body[0].value.args[0].s

# 在真正開始爬網頁前先檢查功能是否開啟

def ceiba_function_enabled(request, course_sn, function, path):
    frame_path = '/modules/index.php'
    frame_args = {'csn': course_sn, 'default_fun': function}
    request.web(frame_path, args=frame_args)
    return len(request.web(path).xpath('//table')) > 0

# 基本的檔案型別：普通檔案、目錄、內部連結、外部連結

class File:
    def __init__(self, vfs, parent):
        self.parent = parent
        self.vfs = vfs
        self.local = True
        self._ready = False

    def fetch(self):
        raise NotImplementedError('繼承的類別沒有實作 fetch 方法')

    def read(self, output, progress_callback=lambda *x: None):
        raise NotImplementedError('繼承的類別沒有實作 read 方法')

    @property
    def ready(self):
        return self._ready

    @ready.setter
    def ready(self, value):
        if value == True:
            self._ready = True
        else:
            raise ValueError('不可以將 ready 重設為 False 或任何其他數值')

class Regular(File):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)

    def read(self, output, progress_callback=lambda *x: None):
        progress_callback(False, None, None, None)
        if not self.ready:
            self.fetch()
        output.write(self._content.encode())
        progress_callback(True, None, None, None)

    def size(self):
        return len(self._content.encode())

class Directory(File):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
        self._children = list()

    def read(self, output, **kwargs):
        if not self.ready:
            self.fetch()
        output.write(str(self._children).encode() + b'\n')

    def list(self):
        if not self.ready:
            self.fetch()
        return self._children

    def add(self, name, node, ignore_duplicate=False):
        assert node.parent is self
        assert node.vfs is self.vfs
        assert len(name) > 0
        name = name.replace('/', '_').strip()
        if name in map(lambda x: x[0], self._children):
            if ignore_duplicate:
                return
            else:
                raise FileExistsError('檔案 {} 已經存在了'.format(name))
        self._children.append((name, node))

    def access(self, name):
        if name == '.':
            return self
        elif name == '..':
            return self.parent
        else:
            if not self.ready:
                self.fetch()
            for child in self._children:
                if child[0] == name:
                    return child[1]
            raise FileNotFoundError('在目前的目錄下找不到 {} 檔案'.format(name))

    def unlink(self, name):
        for index, child in enumerate(self._children):
            if child[0] == name:
                del self._children[index]
                return
        raise FileNotFoundError('在目前的目錄下找不到 {} 檔案'.format(name))


class InternalLink(File):
    def __init__(self, vfs, parent, path):
        super().__init__(vfs, parent)
        self.path = path
        self.ready = True

    def access(self, name):
        return self.vfs.open(self.path, cwd=self.parent).access(name)

    def read(self, *args, **kwargs):
        return self.vfs.open(self.path, cwd=self.parent).read(*args, **kwargs)

    def read_link(self):
        return self.path

class ExternalLink(File):
    def __init__(self, vfs, parent, url):
        super().__init__(vfs, parent)
        self.url = url
        self.ready = True

    def read(self, output, **kwargs):
        output.write(self.url.encode() + b'\n')

    def read_link(self):
        return self.url

# 其他衍生的檔案型別

class RootDirectory(Directory):
    def __init__(self, vfs):
        super().__init__(vfs, self)
        s = self.vfs.strings
        self._courses = RootCoursesDirectory(self.vfs, self)
        self._students = RootStudentsDirectory(self.vfs, self)
        self._teachers = RootTeachersDirectory(self.vfs, self)
        self.add(s['dir_root_courses'], self._courses)
        self.add(s['dir_root_students'], self._students)
        self.add(s['dir_root_teachers'], self._teachers)
        self.ready = True

    @property
    def courses(self):
        return self._courses

    @property
    def students(self):
        return self._students

    @property
    def teachers(self):
        return self._teachers

class RootCoursesDirectory(Directory):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)

    def fetch(self):
        s = self.vfs.strings
        result = self.vfs.request.api({'mode': 'semester'})
        for semester in result['semester']:
            name = semester['semester']
            if 'now' in semester:
                assert semester['now'] == 1
                self.add(s['link_semester_current'],
                    InternalLink(self.vfs, self, name))
            self.add(name, SemesterDirectory(self.vfs, self, name))
        self.ready = True

    def _create_course_list_map(self):
        course_list_page = self.vfs.request.web('/student/index.php')
        course_list_rows_all = course_list_page.xpath('//table[1]/tr')
        course_list_rows = course_list_rows_all[1:]
        course_list_header_row = course_list_rows_all[0]

        assert len(course_list_header_row) == 8
        assert course_list_header_row[0].text in ['學期', 'Semester']
        assert course_list_header_row[1].text in ['授課對象', 'Designated for']
        assert course_list_header_row[2].text in ['課號', 'Course No']
        assert course_list_header_row[3].text in ['班次', 'Class']
        assert course_list_header_row[4].text in ['課程名稱', 'Course Title']
        assert course_list_header_row[5].text in ['教師', 'Instructor']
        assert course_list_header_row[6].text in ['課程助教', 'TA']
        assert course_list_header_row[7].text in ['網頁助教', 'Web Assistant']

        self._course_list_map = dict()
        for row in course_list_rows:
            assert len(row[4]) == 2
            assert row[4][0].tag == 'a'
            assert row[4][0].get('href')
            assert row[4][1].tag == 'br'

            course_path = url_to_path_and_args(row[4][0].get('href'))[0]
            location = self.vfs.request.web_redirect(course_path)
            assert location

            redirected_path, redirected_args = url_to_path_and_args(location)
            if redirected_path == '/login_test.php':
                assert set(redirected_args.keys()) == set(['csn'])
                self._course_list_map[redirected_args['csn']] = row
            elif redirected_path.startswith('/course/') and \
                redirected_path.endswith('/index.htm'):
                assert redirected_args == {}
                self._course_list_map[redirected_path.split('/')[2]] = row
            else:
                assert False

    def search_course_list(self, sn):
        if not hasattr(self, '_course_list_map'):
            self._create_course_list_map()
        return self._course_list_map[sn]

class RootStudentsDirectory(Directory):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
        self._course_status_cache = dict()
        self.ready = True

    def access(self, name):
        if name not in ['.', '..'] and \
            name not in map(lambda x: x[0], self._children):
            self.add_student(name)
        return super().access(name)

    def _is_student_function_enabled(self, sn):
        if sn not in self._course_status_cache:
            self._course_status_cache[sn] = \
                ceiba_function_enabled(self.vfs.request, sn,
                    'student', '/modules/student/stu_person.php')
        return self._course_status_cache[sn]

    def add_student(self, account, sn=None, pwd=None):
        s = self.vfs.strings

        if sn and (not hasattr(self, '_last_sn') or sn != self._last_sn) and \
            self._is_student_function_enabled(sn):
            self._last_sn = sn

        if hasattr(self, '_last_sn'):
            if hasattr(self, '_queued_addition_requests'):
                queued_addition_requests = self._queued_addition_requests.keys()
                del self._queued_addition_requests
                for request in queued_addition_requests:
                    self.add(request, StudentsStudentDirectory(
                        self.vfs, self, request))
            if hasattr(self, '_queued_deletion_requests'):
                queued_deletion_requests = self._queued_deletion_requests.keys()
                del self._queued_deletion_requests
                for request in queued_deletion_requests:
                    node = self.vfs.open(request, edit_check=False)
                    node.parent.unlink(PurePosixPath(request).name)
            if account not in map(lambda x: x[0], self._children):
                self.add(account, StudentsStudentDirectory(
                    self.vfs, self, account))
        else:
            if not hasattr(self, '_queued_addition_requests'):
                self._queued_addition_requests = OrderedDict()
            self._queued_addition_requests[account] = None

        if pwd:
            depth = 0
            while pwd is not self.vfs.root:
                pwd = pwd.parent
                depth += 1
            return PurePosixPath('../' * depth,
                s['dir_root_students'], account).as_posix()

    def queue_deletion_request(self, path):
        assert not hasattr(self, '_last_sn')
        if not hasattr(self, '_queued_deletion_requests'):
            self._queued_deletion_requests = OrderedDict()
        self._queued_deletion_requests[path] = None

    @property
    def last_sn(self):
        return self._last_sn

class RootTeachersDirectory(Directory):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
        self._is_teacher_cache = dict()
        self.ready = True

    def access(self, name):
        if name not in ['.', '..'] and \
            name not in map(lambda x: x[0], self._children) and \
            self.is_teacher(name):
            self.add_teacher(name)
        return super().access(name)

    def add_teacher(self, account, pwd=None):
        s = self.vfs.strings
        if account not in map(lambda x: x[0], self._children):
            self.add(account, TeachersTeacherDirectory(self.vfs, self, account))
        if pwd:
            depth = 0
            while pwd is not self.vfs.root:
                pwd = pwd.parent
                depth += 1
            return PurePosixPath('../' * depth,
                s['dir_root_teachers'], account).as_posix()

    def is_teacher(self, account):
        if account not in self._is_teacher_cache:
            teacher_path = '/student/teacher.php'
            teacher_args = {'op': 's2', 'td': account}
            self._is_teacher_cache[account] = len(self.vfs.request.web(
                teacher_path, args=teacher_args).xpath('//table')) > 0
        return self._is_teacher_cache[account]

class TeachersTeacherDirectory(Directory):
    def __init__(self, vfs, parent, account):
        super().__init__(vfs, parent)
        self._account = account

    def fetch(self):
        s = self.vfs.strings
        teacher_path = '/student/teacher.php'
        teacher_args = {'op': 's2', 'td': self._account}
        teacher_page = self.vfs.request.web(teacher_path, args=teacher_args)

        if len(teacher_page.xpath('//table')) == 0:
            self.add('{}.txt'.format(self._account), StringFile(
                self.vfs, self, '此老師無計中帳號資料！！\n'))
            self.ready = True
            return

        teacher_rows = teacher_page.xpath('/html/body/table[2]/tr/td/table/tr')
        assert len(teacher_rows) == 9

        teacher_file = JSONFile(self.vfs, self)
        teacher_filename = '{}.json'.format(self._account)
        self.add(teacher_filename, teacher_file)

        # 姓名
        teacher_name = row_get_value(
            teacher_rows[0], ['姓名：', 'Name：'],
            {}, free_form=True, is_teacher_page=True)
        assert len(teacher_name) == 1
        teacher_name = teacher_name[0].strip()
        teacher_file.add(s['attr_teachers_name'], teacher_name, teacher_path)

        # 所屬院系所
        teacher_department = row_get_value(
            teacher_rows[1], ['所屬院系所：', 'College & Dept：'],
            {}, free_form=True, is_teacher_page=True)
        assert len(teacher_department) == 1
        teacher_department = ' '.join(teacher_department[0].split())
        teacher_file.add(s['attr_teachers_department'],
            teacher_department, teacher_path)

        # 職稱
        teacher_title = row_get_value(
            teacher_rows[2], ['職稱：', 'Position：'],
            {}, free_form=True, is_teacher_page=True)
        assert len(teacher_title) == 1
        teacher_title = teacher_title[0].strip()
        teacher_file.add(s['attr_teachers_title'], teacher_title, teacher_path)

        # 個人首頁網址
        teacher_url = row_get_value(
            teacher_rows[3], ['個人首頁網址：', 'Homepage URL：'],
            {}, free_form=True, is_teacher_page=True)
        assert len(teacher_url) == 1
        teacher_url = teacher_url[0].strip()
        teacher_file.add(s['attr_teachers_url'], teacher_url, teacher_path)

        # 電子郵件
        teacher_email = row_get_value(
            teacher_rows[4], ['電子郵件：', 'Email：'],
            {}, free_form=True, is_teacher_page=True)
        assert len(teacher_email) == 1
        teacher_email = teacher_email[0].strip()
        teacher_file.add(s['attr_teachers_email'], teacher_email, teacher_path)

        # 聯絡電話
        teacher_phones = row_get_value(
            teacher_rows[5], ['聯絡電話：', 'Phone：'],
            {}, free_form=True, is_teacher_page=True)
        if len(teacher_phones) >= 2:
            assert len(teacher_phones) == 3
            assert teacher_phones[2].isspace()
            teacher_phone_others = ' '.join(teacher_phones[1].split())
            assert teacher_phone_others.startswith('其他電話:')
            teacher_phone_others = teacher_phone_others[5:]
        else:
            teacher_phone_others = ''
        assert len(teacher_phones) >= 1
        teacher_phone = ' '.join(teacher_phones[0].split())
        teacher_file.add(s['attr_teachers_phone'], teacher_phone, teacher_path)
        teacher_file.add(s['attr_teachers_phone_others'],
            teacher_phone_others, teacher_path)

        # 辦公室：
        teacher_office = row_get_value(
            teacher_rows[6], ['辦公室：', 'Office：'],
            {}, free_form=True, is_teacher_page=True)
        assert len(teacher_office) == 1
        teacher_office = ' '.join(teacher_office[0].split())
        teacher_file.add(s['attr_teachers_office'], teacher_office, teacher_path)

        # 照片
        teacher_picture_element = row_get_value(
            teacher_rows[7], ['照片：', 'Photo：'],
            {}, free_form=True, is_teacher_page=True, return_object=True)
        assert len(teacher_picture_element) == 1
        assert teacher_picture_element[0].tag == 'font'
        if len(teacher_picture_element[0]) > 0:
            assert len(teacher_picture_element[0]) == 1
            assert teacher_picture_element[0][0].tag == 'img'
            assert teacher_picture_element[0][0].get('src')
            teacher_picture = teacher_picture_element[0][0].get('src') \
                .rsplit('/', maxsplit=1)[1]
            teacher_picture_path = '{}/{}'.format(
                teacher_path.rsplit('/', maxsplit=1)[0],
                teacher_picture_element[0][0].get('src'))
            self.add(teacher_picture, DownloadFile(self.vfs, self,
                teacher_picture_path))
        else:
            teacher_picture = ''
        teacher_file.add(s['attr_teachers_picture'],
            teacher_picture, teacher_path)

        # 更多的個人資訊
        teacher_more_element = row_get_value(teacher_rows[8], ['\xa0'],
            {}, free_form=True, is_teacher_page=True, return_object=True)
        assert list(map(lambda x: x.tag, teacher_more_element)) == \
            ['br'] * len(teacher_more_element)
        teacher_more = ''.join(teacher_more_element.itertext()).replace('\r', '\n')
        teacher_file.add(s['attr_teachers_more'], teacher_more, teacher_path)

        teacher_file.finish()
        self.ready = True

class StudentsStudentDirectory(Directory):
    def __init__(self, vfs, parent, account):
        super().__init__(vfs, parent)
        self._account = account

    def fetch(self):
        s = self.vfs.strings
        sn = self.vfs.root.students.last_sn

        frame_path = '/modules/index.php'
        frame_args = {'csn': sn, 'default_fun': 'info'}

        student_path = '/modules/student/stu_person.php'
        student_args = {'stu': self._account}

        self.vfs.request.web(frame_path, args=frame_args)
        student_page = self.vfs.request.web(student_path, args=student_args)
        assert len(student_page.xpath('//table')) > 0

        student_rows = student_page.xpath('//div[@id="sect_cont"]/table/tr')
        assert len(student_rows) == 12

        student_file = JSONFile(self.vfs, self)
        student_filename = '{}.json'.format(self._account, sn)
        self.add(student_filename, student_file)

        # 身份
        student_role = row_get_value(student_rows[0],
            ['身份', 'Role'], {}, free_form=True).strip()
        student_file.add(s['attr_students_role'], student_role, student_path)

        # 照片
        student_photo_element = row_get_value(student_rows[1],
            ['照片', 'Photo'], {}, free_form=True, return_object=True)
        if len(student_photo_element) > 0:
            assert len(student_photo_element) == 1
            assert student_photo_element[0].tag == 'img'
            assert student_photo_element[0].get('src')
            student_photo = student_photo_element[0].get('src') \
                .rsplit('/', maxsplit=1)[1]
            student_photo_path = url_to_path_and_args(
                student_photo_element[0].get('src'), no_query_string=True)[0]
            self.add(student_photo, DownloadFile(self.vfs, self,
                student_photo_path))
        else:
            student_photo = ''
        student_file.add(s['attr_students_photo'], student_photo, student_path)

        # 姓名
        student_name = row_get_value(student_rows[2],
            ['姓名', 'Name'], {}, free_form=True).strip()
        student_file.add(s['attr_students_name'], student_name, student_path)

        # 英文姓名
        student_english_name = row_get_value(student_rows[3],
            ['英文姓名', 'English Name'], {}, free_form=True).strip()
        student_file.add(s['attr_students_english_name'],
            student_english_name, student_path)

        # 匿名代號
        student_screen_name = row_get_value(student_rows[4],
            ['匿名代號', 'Screen Name'], {}, free_form=True).strip()
        student_file.add(s['attr_students_screen_name'],
            student_screen_name, student_path)

        # 學校系級
        student_school_year = row_get_value(student_rows[5],
            ['系級', 'Major & Year', '學校系級', 'School & Dept'],
            {}, free_form=True).strip()
        student_file.add(s['attr_students_school_year'],
            student_school_year, student_path)

        # 個人首頁網址
        student_homepage_url_element = row_get_value(student_rows[6],
            ['個人首頁網址', 'Homepage URL'], {}, free_form=True, return_object=True)
        assert len(student_homepage_url_element) == 1
        assert student_homepage_url_element[0].tag == 'a'
        assert student_homepage_url_element[0].get('href')
        student_homepage_url = element_get_text(student_homepage_url_element[0])
        assert student_homepage_url_element[0].get('href') == \
            student_homepage_url or \
            student_homepage_url_element[0].get('href') == \
            'http://' + student_homepage_url
        student_file.add(s['attr_students_homepage_url'],
            student_homepage_url, student_path)

        # 電子郵件
        student_email_address_element = row_get_value(student_rows[7],
            ['電子郵件', 'Email Address'], {}, free_form=True, return_object=True)
        assert len(student_email_address_element) == 1
        assert student_email_address_element[0].tag == 'a'
        assert student_email_address_element[0].get('href')
        student_email_address = element_get_text(student_email_address_element[0])
        assert student_email_address_element[0].get('href') == \
            'mailto:' + student_email_address
        student_file.add(s['attr_students_email_address'],
            student_email_address, student_path)

        # 常用電子郵件
        student_frequently_used_email_element = row_get_value(student_rows[8],
            ['常用電子郵件', 'Frequently Used Email'],
            {}, free_form=True, return_object=True)
        assert len(student_frequently_used_email_element) == 1
        assert student_frequently_used_email_element[0].tag == 'a'
        assert student_frequently_used_email_element[0].get('href')
        student_frequently_used_email = element_get_text(
            student_frequently_used_email_element[0])
        student_frequently_used_email_from_href = \
            student_frequently_used_email_element[0].get('href')

        # CEIBA 不會跳脫 < 和 > 符號，如果使用者填寫的電子郵件地址包含這個符號
        # 會使透過 .text 拿到的資料不正確
        if student_frequently_used_email_from_href.find('<') >= 0 and \
            student_frequently_used_email_from_href.find('>') >= 0:
            assert student_frequently_used_email_from_href.startswith('mailto:')
            student_frequently_used_email = \
                student_frequently_used_email_from_href[7:]
        else:
            assert student_frequently_used_email_from_href == \
                'mailto:' + student_frequently_used_email

        student_file.add(s['attr_students_frequently_used_email'],
            student_frequently_used_email, student_path)

        # 聯絡電話
        student_phone = row_get_value(student_rows[9],
            ['聯絡電話', 'Phone'], {}, free_form=True).strip()
        student_file.add(s['attr_students_phone'], student_phone, student_path)

        # 聯絡地址
        student_address = row_get_value(student_rows[10],
            ['聯絡地址', 'Address'], {}, free_form=True).strip()
        student_file.add(s['attr_students_address'],
            student_address, student_path)

        # 更多的個人資訊
        student_more_personal_information_element = row_get_value(student_rows[11],
            ['更多的個人資訊', 'More Personal Information'],
            {}, free_form=True, return_object=True)

        # 使用者可以自己在這個欄位塞各種標籤……
        student_more_personal_information = ''.join(
            student_more_personal_information_element.itertext())
        student_file.add(s['attr_students_more_personal_information'],
            student_more_personal_information, student_path)

        student_file.finish()
        self.ready = True

class SemesterDirectory(Directory):
    def __init__(self, vfs, parent, semester):
        super().__init__(vfs, parent)
        self._semester = semester

    def fetch(self):
        result = self.vfs.request.api(
            {'mode': 'semester', 'semester': self._semester})
        result_keys = ['student_id', 'student_cname', 'semester', 'grid', 'calendar']
        assert set(result.keys()) == set(result_keys)

        days = '一二三四五六日'
        slots = '01234@56789XABC'
        courses = dict()

        class Course(dict):
            def __init__(self):
                super().__init__(self)
                self['sn'] = None
                self['time'] = list()
                self['class_no'] = ''

        for course in result['calendar']:
            name = course['crs_cname']
            sn = course['course_sn']
            day = days[course['day']]
            slot = list(course['slot'])
            assert isinstance(name, str)
            assert isinstance(sn, str)
            assert slot == sorted(slot, key=lambda x: slots.index(x))

            if name in courses:
                assert sn == courses[name]['sn']
            else:
                courses[name] = Course()
            courses[name]['sn'] = sn
            courses[name]['time'].append((day, slot))
            courses[name]['class_no'] = ''

        for ceiba_course in result['grid']:
            class_no = ceiba_course['class_no']
            sn = ceiba_course['course_sn']
            name = ceiba_course['crs_cname']
            semester = ceiba_course['semester']
            assert semester == self._semester

            if class_no == 0 and sn == 0 and name == 'Calendar':
                continue

            assert isinstance(class_no, str)
            if name in courses:
                assert sn == courses[name]['sn']
            else:
                courses[name] = Course()
            courses[name]['sn'] = sn
            courses[name]['class_no'] = class_no

        for name, course in courses.items():
            self.add(name, CourseDirectory(self.vfs, self, self._semester,
                name, course['sn'], course['time'], course['class_no']))

        for name, course in courses.items():
            if course['sn'] != '':
                self.add(course['sn'], InternalLink(self.vfs, self, name))

        self.ready = True

class CourseDirectory(Directory):
    def __init__(self, vfs, parent, semester, name, sn, time, class_no):
        super().__init__(vfs, parent)
        self._semester = semester
        self._name = name
        self._sn = sn
        self._time = time
        self._class_no = class_no

    def fetch(self):
        # 填入課程基本資料
        s = self.vfs.strings
        metadata = JSONFile(self.vfs, self)
        self.add(s['file_course_metadata'], metadata)
        metadata.add(s['attr_course_metadata_name'], self._name, 'crs_cname')
        metadata.add(s['attr_course_metadata_class_no'], self._class_no, 'class_no')
        if len(self._sn) > 0:
            metadata.add(s['attr_course_metadata_sn'], self._sn, 'course_sn')
        if len(self._time) > 0:
            metadata.add(s['attr_course_metadata_time'], self._time, ['day', 'slot'])

        # 沒有 CEIBA 代號的現在可以離開了
        if len(self._sn) == 0:
            metadata.finish()
            self.ready = True
            return

        # CEIBA 規定要先呼叫過 semester 才能用 course
        self.vfs.request.api({'mode': 'semester', 'semester': self._semester})
        # 一定要傳 class_no 才能拿到完整的課程內容
        result = self.vfs.request.api(
            {'mode': 'course',
             'semester': self._semester,
             'course_sn': self._sn,
             'class_no': self._class_no})
        result_keys = [
            'lang', 'course_info', 'teacher_info', 'contents', 'content_files']
        optional_keys = [
            'bulletin', 'board', 'course_grade', 'homeworks']
        assert set(result.keys()) - set(optional_keys) == set(result_keys)

        # lang
        if result['lang'] == 'big5':
            lang = s['value_course_metadata_lang_big5']
        elif result['lang'] == 'eng':
            lang = s['value_course_metadata_lang_eng']
        else:
            assert False, '無法識別的課程語言 {}'.format(result['lang'])

        metadata.add(s['attr_course_metadata_lang'], lang, 'lang')

        # course_info
        for day in range(1, 7):
            day_attr = 'day{}'.format(day)
            if day_attr in result['course_info']:
                del result['course_info'][day_attr]
        course_info_keys = [ 'course_req', 'dpt_cou', 'mark', 'place' ]
        assert set(result['course_info'].keys()) == set(course_info_keys)

        metadata.add(s['attr_course_metadata_code'], result['course_info']['dpt_cou'], 'dpt_cou')
        metadata.add(s['attr_course_metadata_place'], result['course_info']['place'], 'place')
        if len(result['course_info']['mark']) > 0:
            metadata.add(s['attr_course_metadata_mark'], result['course_info']['mark'], 'mark')
        if len(result['course_info']['course_req']) > 0:
            evaluation = list()
            for req in result['course_info']['course_req']:
                req_keys = ['item', 'percent', 'notes']
                assert set(req.keys()) == set(req_keys)
                req_data = OrderedDictWithLineBreak()
                req_data[s['attr_course_metadata_evaluation_item']] = req['item']
                req_data[s['attr_course_metadata_evaluation_percent']] = req['percent']
                req_data[s['attr_course_metadata_evaluation_notes']] = req['notes']
                evaluation.append(req_data)
            metadata.add(s['attr_course_metadata_evaluation'], evaluation, 'course_req')
        metadata.finish()

        # bulletin
        if 'bulletin' in result:
            self.add(s['dir_course_bulletin'], CourseBulletinDirectory(
                self.vfs, self, self._sn, result['bulletin']))

        # contents + content_files
        self.add(s['dir_course_contents'], CourseContentsDirectory(
            self.vfs, self, self._sn, result['contents'], result['content_files']))

        # board
        if 'board' in result:
            assert result['board'] == '1'
            self.add(s['dir_course_boards'], CourseBoardsDirectory(
                self.vfs, self, self._semester, self._sn))

        # homeworks
        if 'homeworks' in result:
            self.add(s['dir_course_homeworks'], CourseHomeworksDirectory(
                self.vfs, self, self._sn, result['homeworks']))

        # course_grade
        if 'course_grade' in result:
            self.add(s['dir_course_grades'], CourseGradesDirectory(
                self.vfs, self, self._sn, result['course_grade']))

        # 資源分享
        if ceiba_function_enabled(self.vfs.request, self._sn,
            'share', '/modules/share/share.php'):
            self.add(s['dir_course_share'], CourseShareDirectory(
                self.vfs, self, self._sn))

        # 投票區
        if ceiba_function_enabled(self.vfs.request, self._sn,
            'vote', '/modules/vote/vote.php'):
            self.add(s['dir_course_vote'], CourseVoteDirectory(
                self.vfs, self, self._sn))

        # teacher_info
        self.add(s['dir_course_teachers'], CourseTeacherInfoDirectory(
            self.vfs, self, self._sn, result['teacher_info']))

        # 修課學生
        self.add(s['dir_course_students'], CourseRosterDirectory(
            self.vfs, self, self._sn))

        # 課程助教
        course_list_row = self.vfs.root.courses.search_course_list(self._sn)
        if len(course_list_row[6]) > 0:
            self.add(s['dir_course_teaching_assistants'],
                CourseAssistantsDirectory(self.vfs, self, course_list_row[6]))

        # 網頁助教
        if len(course_list_row[7]) > 0:
            self.add(s['dir_course_web_assistants'],
                CourseAssistantsDirectory(self.vfs, self, course_list_row[7]))

        self.ready = True

class WebCourseDirectory(Directory):
    def __init__(self, vfs, parent, semester, sn):
        super().__init__(vfs, parent)
        self._semester = semester
        self._sn = sn

    def fetch(self):
        s = self.vfs.strings

        frame_path = '/modules/index.php'
        frame_args = {'csn': self._sn, 'default_fun': 'info'}

        info_path = '/modules/info/info.php'

        self.vfs.request.web(frame_path, args=frame_args)
        info_page = self.vfs.request.web(info_path)
        info_basic_rows = info_page.xpath('//div[@id="sect_cont"]/table[1]/tr')

        metadata = JSONFile(self.vfs, self)

        # name
        info_basic_name = row_get_value(info_basic_rows[0],
            ['課程名稱', 'Course Name'], {}, free_form=True)
        metadata.add(s['attr_course_metadata_name'], info_basic_name, info_path)
        self._name = info_basic_name

        info_basic_semester = row_get_value(info_basic_rows[1],
            ['開課學期', 'Semester'], {}, free_form=True)
        if info_basic_semester != self._semester:
            self.vfs.logger.warning(
                '手動加入的課程 {} （{}）開課學期是 {}，卻被放在 {} 資料夾' \
                .format(self._sn, self._name, info_basic_semester, self._semester))

        # class_no
        info_basic_class_no = row_get_value(info_basic_rows[4],
            ['班次', 'Class'], {}, free_form=True)
        metadata.add(s['attr_course_metadata_class_no'],
            info_basic_class_no, info_path)

        # sn
        metadata.add(s['attr_course_metadata_sn'], self._sn, '設定檔')

        # time
        info_basic_time = row_get_value(info_basic_rows[5],
            ['上課時間', 'Time'], {}, free_form=True)
        metadata.add(s['attr_course_metadata_time'], info_basic_time, info_path)

        # code
        info_basic_code = row_get_value(info_basic_rows[3],
            ['課號', 'Course No.'], {}, free_form=True)
        metadata.add(s['attr_course_metadata_code'], info_basic_code, info_path)

        # place
        info_basic_place = row_get_value(info_basic_rows[6],
            ['上課地點', 'Classroom'], {}, free_form=True)
        metadata.add(s['attr_course_metadata_place'], info_basic_place, info_path)

        metadata.finish()
        self.add(s['file_course_metadata'], metadata)

        # homeworks
        if ceiba_function_enabled(self.vfs.request, self._sn,
            'hw', '/modules/hw/hw.php'):
            self.add(s['dir_course_homeworks'], CourseHomeworksDirectory(
                self.vfs, self, self._sn, [], api=False))

        # course_grade
        if ceiba_function_enabled(self.vfs.request, self._sn,
            'grade', '/modules/grade/grade.php'):
            self.add(s['dir_course_grades'], CourseGradesDirectory(
                self.vfs, self, self._sn, []))

        # 資源分享
        if ceiba_function_enabled(self.vfs.request, self._sn,
            'share', '/modules/share/share.php'):
            self.add(s['dir_course_share'], CourseShareDirectory(
                self.vfs, self, self._sn))

        # 投票區
        if ceiba_function_enabled(self.vfs.request, self._sn,
            'vote', '/modules/vote/vote.php'):
            self.add(s['dir_course_vote'], CourseVoteDirectory(
                self.vfs, self, self._sn))

        # 教師資訊
        self.add(s['dir_course_teachers'], CourseTeacherInfoDirectory(
            self.vfs, self, self._sn, []))

        # 修課學生
        self.add(s['dir_course_students'], CourseRosterDirectory(
            self.vfs, self, self._sn))

        # 課程助教
        course_list_row = self.vfs.root.courses.search_course_list(self._sn)
        if len(course_list_row[6]) > 0:
            self.add(s['dir_course_teaching_assistants'],
                CourseAssistantsDirectory(self.vfs, self, course_list_row[6]))

        # 網頁助教
        if len(course_list_row[7]) > 0:
            self.add(s['dir_course_web_assistants'],
                CourseAssistantsDirectory(self.vfs, self, course_list_row[7]))

        self.ready = True

    @property
    def name(self):
        return self._name

class CourseBulletinDirectory(Directory):
    def __init__(self, vfs, parent, course_sn, bulletin):
        super().__init__(vfs, parent)
        self._course_sn = course_sn
        self._bulletin = bulletin

    def fetch(self):
        s = self.vfs.strings
        anno_keys = ['sn', 'subject', 'post_time', 'b_link', 'attach', 'content']
        attachments_directory_created = False
        for anno in self._bulletin:
            assert set(anno.keys()) == set(anno_keys)
            anno_node = JSONFile(self.vfs, self)
            anno_node.add(s['attr_course_bulletin_sn'], anno['sn'], 'sn')
            anno_node.add(s['attr_course_bulletin_subject'], anno['subject'], 'subject')
            anno_node.add(s['attr_course_bulletin_date'], anno['post_time'], 'post_time')
            anno_node.add(s['attr_course_bulletin_url'], anno['b_link'], 'b_link')
            anno_node.add(s['attr_course_bulletin_attachment'], anno['attach'], 'attach')
            anno_node.add(s['attr_course_bulletin_content'], anno['content'], 'content')
            anno_node.finish()
            anno_filename = format_filename(anno['sn'], anno['subject'], 'json')
            self.add(anno_filename, anno_node)
            if len(anno['attach']) > 0:
                if not attachments_directory_created:
                    att_dir = Directory(self.vfs, self)
                    attachments_directory_created = True
                att_dir.add(anno['attach'], DownloadFile(
                    self.vfs, att_dir, '/course/{}/bulletin/{}'.format(
                        self._course_sn, anno['attach'])),
                    ignore_duplicate=True)
        if attachments_directory_created:
            att_dir.ready = True
            self.add(s['dir_course_bulletin_attachments'], att_dir)
        self.ready = True

class CourseContentsDirectory(Directory):
    def __init__(self, vfs, parent, course_sn, contents, content_files):
        super().__init__(vfs, parent)
        self._course_sn = course_sn
        self._contents = contents
        self._content_files = content_files

    def fetch(self):
        s = self.vfs.strings
        content_keys = ['syl_sn', 'unit', 'notes', 'subject']
        content_file_keys = ['syl_sn', 'file_name']

        all_contents = OrderedDict()

        for content in self._contents:
            if isinstance(content, list):
                assert len(content) == 4
                assert len(self._contents) == 1
                break
            assert set(content.keys()) == set(content_keys)
            content_node = JSONFile(self.vfs, self)
            content_node.add(s['attr_course_contents_sn'], content['syl_sn'], 'syl_sn')
            content_node.add(s['attr_course_contents_week'], content['unit'], 'unit')
            content_node.add(s['attr_course_contents_date'], content['notes'], 'notes')
            content_node.add(s['attr_course_contents_subject'], content['subject'], 'subject')
            content_node.add(s['attr_course_contents_files'], list(), 'file_name')
            all_contents[content['syl_sn']] = content_node

        for content_file in self._content_files:
            assert set(content_file.keys()) == set(content_file_keys)
            all_contents[content_file['syl_sn']].append(
                s['attr_course_contents_files'], content_file['file_name'])

        for sn, content_node in all_contents.items():
            content_filename = format_filename(sn,
                content_node.get(s['attr_course_contents_week']), 'json')
            content_node.finish()
            self.add(content_filename, content_node)

        if len(self._content_files) > 0:
            files_dir = Directory(self.vfs, self)
            for content_file in self._content_files:
                files_dir.add(content_file['file_name'], DownloadFile(
                    self.vfs, files_dir, '/course/{}/content/{}'.format(
                        self._course_sn, content_file['file_name'])),
                    ignore_duplicate=True)
            files_dir.ready = True
            self.add(s['dir_course_contents_files'], files_dir)

        self.ready = True

class CourseBoardsDirectory(Directory):
    def __init__(self, vfs, parent, semester, course_sn):
        super().__init__(vfs, parent)
        self._semester = semester
        self._course_sn = course_sn

    def fetch(self):
        # CEIBA 規定要先呼叫過 semester 才能用 read_board
        self.vfs.request.api({'mode': 'semester', 'semester': self._semester})
        result = self.vfs.request.api(
            {'mode': 'read_board',
             'semester': self._semester,
             'course_sn': self._course_sn,
             'board': '0'})
        board_keys = ['sn', 'caption']

        for board in result:
            assert set(board.keys()) == set(board_keys)
            posts_dir = CourseBoardsThreadDirectory(self.vfs, self,
                self._semester, self._course_sn, board)
            posts_dirname = format_dirname(board['sn'], board['caption'])
            self.add(posts_dirname, posts_dir)

        self.ready = True

class CourseBoardsThreadDirectory(Directory):
    def __init__(self, vfs, parent, semester, course_sn, board):
        super().__init__(vfs, parent)
        self._semester = semester
        self._course_sn = course_sn
        self._board = board

    def fetch(self):
        s = self.vfs.strings

        board_metadata = JSONFile(self.vfs, self)
        board_metadata.add(s['attr_course_boards_metadata_sn'],
            self._board['sn'], 'sn')
        board_metadata.add(s['attr_course_boards_metadata_caption'],
            self._board['caption'], 'caption')
        board_metadata.finish()
        self.add(s['file_course_boards_metadata'], board_metadata)

        # CEIBA 規定要先呼叫過 semester 才能用 read_board_post
        self.vfs.request.api({'mode': 'semester', 'semester': self._semester})
        result = self.vfs.request.api(
            {'mode': 'read_board_post',
             'semester': self._semester,
             'course_sn': self._course_sn,
             'board': self._board['sn']})
        post_keys = ['sn', 'parent', 'subject', 'post_time', 'attach', 'file_path',
            'content', 'author', 'cauthor', 'count_rep', 'latest_rep']

        threads = OrderedDict()
        thread_subjects = dict()

        for post in result:
            assert set(post.keys()) == set(post_keys)
            if post['parent'] == '0':
                assert post['sn'] not in thread_subjects
                if post['sn'] in threads:
                    threads[post['sn']].append(post)
                else:
                    threads[post['sn']] = [ post ]
                thread_subjects[post['sn']] = post['subject']
            else:
                if post['parent'] in threads:
                    threads[post['parent']].append(post)
                else:
                    threads[post['parent']] = [ post ]

        for sn, thread in threads.items():
            thread_dir = Directory(self.vfs, self)
            thread_attachments = list()
            collected_accounts = OrderedDict()
            for post in thread:
                post_node = JSONFile(self.vfs, thread_dir)
                post_node.add(s['attr_course_boards_thread_sn'], post['sn'], 'sn')
                if post['parent'] != '0':
                    post_node.add(s['attr_course_boards_thread_parent'],
                        post['parent'], 'parent')
                post_node.add(s['attr_course_boards_thread_subject'],
                    post['subject'], 'subject')
                post_node.add(s['attr_course_boards_thread_post_time'],
                    post['post_time'], 'post_time')
                if post['attach'] != '' or post['file_path'] != '':
                    assert post['attach'] != ''
                    extension = post['attach'].rsplit('.', maxsplit=1)[1]
                    post_node.add(s['attr_course_boards_thread_attach'],
                        post['attach'], 'attach')
                    if post['file_path'] != '':
                        assert post['file_path'] == post['sn'] + '.' + extension
                        thread_attachments.append(
                            (post['sn'], post['attach'], post['file_path']))
                post_node.add(s['attr_course_boards_thread_author'],
                    post['author'], 'author')
                collected_accounts[post['author']] = None
                post_node.add(s['attr_course_boards_thread_cauthor'],
                    post['cauthor'], 'cauthor')
                post_node.add(s['attr_course_boards_thread_count_rep'],
                    post['count_rep'], 'count_rep')
                post_node.add(s['attr_course_boards_thread_latest_rep'],
                    post['latest_rep'], 'latest_rep')
                post_node.finish()
                content = '\n'.join([
                    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN"',
                    '  "http://www.w3.org/TR/html4/loose.dtd">',
                    '<html>',
                    '  <head>',
                    '    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">',
                    '    <title>{}</title>'.format(html.escape(post['subject'])),
                    '  </head>',
                    '  <body>',
                    '    <p>',
                    '      {}'.format(post['content']),
                    '    </p>',
                    '  </body>',
                    '</html>' ]) + '\n'
                post_content = StringFile(self.vfs, thread_dir, content)
                post_node_filename = format_filename(
                    post['sn'], post['subject'], 'json')
                post_content_filename = format_filename(
                    post['sn'], post['subject'], 'html')
                thread_dir.add(post_node_filename, post_node)
                thread_dir.add(post_content_filename, post_content)
            for account in collected_accounts.keys():
                if quote(account) != account:
                    continue
                if self.vfs.root.teachers.is_teacher(account):
                    thread_dir.add(account, InternalLink(self.vfs, thread_dir,
                        self.vfs.root.teachers.add_teacher(
                            account, pwd=thread_dir)))
                else:
                    thread_dir.add(account, InternalLink(self.vfs, thread_dir,
                        self.vfs.root.students.add_student(
                            account, sn=self._course_sn, pwd=thread_dir)))
            if len(thread_attachments):
                files_dir = Directory(self.vfs, thread_dir)
                for attachment in thread_attachments:
                    attachment_filename = format_dirname(
                        attachment[0], attachment[1])
                    attachment_path = '/course/{}/board/{}'.format(
                        self._course_sn, attachment[2])
                    files_dir.add(attachment_filename, DownloadFile(
                        self.vfs, files_dir, attachment_path))
                files_dir.ready = True
                thread_dir.add(s['dir_course_boards_thread_files'], files_dir)
            thread_dir.ready = True
            thread_dirname = format_dirname(sn, thread_subjects[sn])
            self.add(thread_dirname, thread_dir)

        self.ready = True

class CourseHomeworksDirectory(Directory):
    def __init__(self, vfs, parent, course_sn, homeworks, api=True):
        super().__init__(vfs, parent)
        self._course_sn = course_sn
        self._homeworks = homeworks
        self._api = api

    def fetch(self):
        if self._api:
            for homework in self._homeworks:
                self.add(format_dirname(homework['sn'], homework['name']),
                    CourseHomeworksHomeworkDirectory(self.vfs, self,
                        self._course_sn, homework, homework['sn']))
        else:
            frame_path = '/modules/index.php'
            frame_args = {'csn': self._course_sn, 'default_fun': 'hw'}

            hw_list_path = '/modules/hw/hw.php'

            self.vfs.request.web(frame_path, args=frame_args)
            hw_list_page = self.vfs.request.web(hw_list_path)

            assert len(hw_list_page.xpath('//table')) > 0

            hw_list_rows_all = hw_list_page.xpath('//div[@id="sect_cont"]/table/tr')
            hw_list_rows = hw_list_rows_all[1:]
            hw_header_row = hw_list_rows_all[0]

            assert hw_header_row[0].text in ['名稱', 'Title']

            for row in hw_list_rows:
                assert row[0].tag == 'td'
                assert len(row[0]) == 1
                assert row[0][0].tag == 'a'
                assert row[0][0].get('href')
                args = url_to_path_and_args(row[0][0].get('href'))[1]
                sn = args['hw_sn']

                if len(row[0][0]) >= 1:
                    assert len(row[0][0]) == 1
                    assert row[0][0][0].tag == 'font'
                    name = element_get_text(row[0][0][0])
                else:
                    name = element_get_text(row[0][0])

                self.add(format_dirname(sn, name),
                    CourseHomeworksHomeworkDirectory(self.vfs, self,
                        self._course_sn, {}, sn))

        self.ready = True

class CourseHomeworksHomeworkDirectory(Directory):
    def __init__(self, vfs, parent, course_sn, hw, hw_sn):
        super().__init__(vfs, parent)
        self._course_sn = course_sn
        self._hw = hw
        self._hw_sn = hw_sn
        if self._hw:
            assert self._hw['sn'] == self._hw_sn

    def fetch(self):
        s = self.vfs.strings
        hw_keys = ['sn', 'name', 'description', 'file_path', 'url',
            'pub_date', 'pub_hour', 'end_date', 'end_hour', 'is_subm', 'hw_scores']
        hw_score_keys = ['course_sn', 'hw_sn', 'hand_time', 'file_path',
            'sw', 'ranking_grade', 'score', 'evaluation']

        # 課程主頁面
        frame_path = '/modules/index.php'
        frame_args = {'csn': self._course_sn, 'default_fun': 'hw'}

        # 作業列表
        hw_list_path = '/modules/hw/hw.php'

        # 作業內容
        hw_show_path = '/modules/hw/hw_show.php'
        hw_show_args = {'hw_sn': self._hw_sn}

        # 作業評語
        hw_eval_path = '/modules/hw/hw_eval.php'
        hw_eval_args = {'hw_sn': self._hw_sn, 'all': '1'}

        # 作業觀摩
        hw_view_path = '/modules/hw/hw_view.php'
        hw_view_args = {'hw_sn': self._hw_sn, 'all': '1'}

        # 按照順序爬網頁
        self.vfs.request.web(frame_path, args=frame_args)
        self.vfs.request.web(hw_list_path)
        hw_show_page = self.vfs.request.web(hw_show_path, args=hw_show_args)
        hw_eval_page = self.vfs.request.web(hw_eval_path, args=hw_eval_args)
        hw_view_page = self.vfs.request.web(hw_view_path, args=hw_view_args)

        def make_date_hour(date, hour):
            assert len(date) == 10
            assert date[0:4].isnumeric()
            assert date[5:7].isnumeric()
            assert date[8:10].isnumeric()
            assert date[4] == '-' and date[7] == '-'
            assert len(hour) == 2
            assert hour.isnumeric()
            return date + ' ' + hour

        def use_unicode_private_areas(s):
            for c in s:
                if ord(c) >= 0xe000 and ord(c) <= 0xf8ff:
                    return True
            return False

        hw_show_rows = hw_show_page.xpath('//div[@id="sect_cont"]/table/tr')
        assert len(hw_show_rows) >= 9

        # 0
        hw_show_name = row_get_value(hw_show_rows[0],
            ['名稱', 'Title'], {}, free_form=True)

        # 1
        hw_show_description_element = row_get_value(hw_show_rows[1],
            ['作業說明', 'Description'], {}, free_form=True, return_object=True)
        assert list(map(lambda x: x.tag, hw_show_description_element)) == \
            ['br'] * len(hw_show_description_element)
        hw_show_description = ''.join(hw_show_description_element.itertext()) \
            .rstrip('\xa0')

        hw_show_description_cannot_be_decoded_with_default_encoding = \
            use_unicode_private_areas(hw_show_description)
        if hw_show_description_cannot_be_decoded_with_default_encoding:
            self.vfs.logger.warning(
                '作業 {} 的說明文字中有 Unicode 私人使用區的字元' \
                .format(hw_show_name))
            self.vfs.logger.warning(
                '這很可能是因為預設編碼無法解讀日文所造成的錯誤')
            self.vfs.logger.warning(
                '準備使用 big5-hkscs 編碼重新讀取網頁')
            hw_show_page = self.vfs.request.web(
                hw_show_path, args=hw_show_args, encoding='big5-hkscs')
            hw_show_rows = hw_show_page.xpath(
                '//div[@id="sect_cont"]/table/tr')
            hw_show_description_element = row_get_value(hw_show_rows[1],
                ['作業說明', 'Description'], {},
                free_form=True, return_object=True)
            hw_show_description = ''.join(
                hw_show_description_element.itertext()).rstrip('\xa0')
            assert not use_unicode_private_areas(hw_show_description)

        # 2
        hw_show_download_file_path_element = row_get_value(hw_show_rows[2],
            ['相關檔案', 'Related File'], {}, free_form=True, return_object=True)
        if len(hw_show_download_file_path_element) > 0:
            assert len(hw_show_download_file_path_element) == 2
            assert hw_show_download_file_path_element[0].tag == 'a'
            assert hw_show_download_file_path_element[0].text in ['檔案', 'File']
            assert hw_show_download_file_path_element[0].get('href')
            assert hw_show_download_file_path_element[1].tag == 'br'
            hw_show_download_file_path_full = url_to_path_and_args(
                hw_show_download_file_path_element[0].get('href'))[0]
            hw_show_download_file_dir, hw_show_download_file_path = \
                hw_show_download_file_path_full.rsplit('/', maxsplit=1)
            assert hw_show_download_file_dir == '/course/{}/hw' \
                .format(self._course_sn)
        else:
            assert hw_show_download_file_path_element.text.isspace()
            hw_show_download_file_path = ''

        # 3
        hw_show_url_element = row_get_value(hw_show_rows[3],
            ['相關網址', 'Related URL'], {}, free_form=True, return_object=True)
        if len(hw_show_url_element) > 0:
            assert len(hw_show_url_element) == 2
            assert hw_show_url_element[0].tag == 'a'
            assert hw_show_url_element[0].get('href')
            assert hw_show_url_element[1].tag == 'br'
            hw_show_url = hw_show_url_element[0].get('href')
            assert hw_show_url == hw_show_url_element[0].text
        else:
            assert hw_show_url_element.text.isspace()
            hw_show_url = ''

        # 4
        hw_show_type = row_get_value(hw_show_rows[4], ['成員', 'Type'],
            {('個人', 'individual'):
                s['value_course_homeworks_homework_type_individual'],
             ('小組', 'group'):
                s['value_course_homeworks_homework_type_group']})

        # 5
        hw_show_method = row_get_value(hw_show_rows[5],
            ['繳交方法', 'Submission Method'],
            {('線上繳交', 'submit online'):
                s['value_course_homeworks_homework_method_online'],
             ('紙本繳交', 'printed copy'):
                s['value_course_homeworks_homework_method_printed']},
            free_form=True)

        # 6
        hw_show_percentage = row_get_value(hw_show_rows[6],
            ['成績比重', 'Submission Method'], {}, free_form=True)

        # 7
        hw_show_end = row_get_value(hw_show_rows[7],
            ['繳交期限', 'Due Date'],
            {('無限期', 'Indefinite Duration'):
                s['value_course_homeworks_homework_end_2030_12_31_24']},
            free_form=True)

        # 8
        hw_show_is_subm = row_get_value(hw_show_rows[8],
            ['逾期繳交', 'Accept late submission after the due date'],
            {('可以', 'Yes'):
                s['attr_course_homeworks_homework_is_subm_yes'],
             ('不可以', 'No'):
                s['attr_course_homeworks_homework_is_subm_no']})

        # 9
        if len(hw_show_rows) > 9:
            hw_show_hand_time = row_get_value(hw_show_rows[9],
                ['繳交日期', 'Submission Date'], {}, free_form=True)
        else:
            hw_show_hand_time = None

        # 10
        need_submitted_files_dir = False
        if len(hw_show_rows) > 10:
            hw_show_submitted_file_path_element = row_get_value(hw_show_rows[10],
                ['已上傳檔案', 'Already Uploaded File'], {},
                free_form=True, return_object=True)
            assert len(hw_show_submitted_file_path_element) == 1
            assert hw_show_submitted_file_path_element[0].tag == 'a'
            assert hw_show_submitted_file_path_element[0].get('href')
            if hw_show_submitted_file_path_element[0].text:
                hw_show_submitted_file_path = \
                    hw_show_submitted_file_path_element[0].text
                need_submitted_files_dir = True
            else:
                hw_show_submitted_file_path = ''
        else:
            hw_show_submitted_file_path = None

        if self._hw:
            assert set(self._hw.keys()) == set(hw_keys)
            assert self._hw['sn'] == self._hw_sn
            assert self._hw['name'] == hw_show_name

            self._hw['description'] = self._hw['description'] \
                .replace('<br>', '').replace('∼', '～')
            if not hw_show_description_cannot_be_decoded_with_default_encoding:
                assert self._hw['description'] == hw_show_description

            assert self._hw['file_path'] == hw_show_download_file_path
            assert self._hw['url'] == hw_show_url

            if self._hw['end_date'] == '2030-12-31' and \
                self._hw['end_hour'] == '24':
                assert hw_show_end == \
                    s['value_course_homeworks_homework_end_2030_12_31_24']
            else:
                assert make_date_hour(self._hw['end_date'], self._hw['end_hour'])

            # XXX: 這看起來是做 CEIBA API 的人弄反了，官方 Android App 上也是錯的
            if self._hw['is_subm'] == '0':
                assert hw_show_is_subm == \
                    s['attr_course_homeworks_homework_is_subm_yes']
            elif self._hw['is_subm'] == '1':
                assert hw_show_is_subm == \
                    s['attr_course_homeworks_homework_is_subm_no']
            else:
                assert False

        hw_node = JSONFile(self.vfs, self)

        # sn
        if self._hw:
            hw_node.add(s['attr_course_homeworks_homework_sn'],
                self._hw['sn'], 'sn')
        else:
            hw_node.add(s['attr_course_homeworks_homework_sn'],
                self._hw_sn, hw_list_path)

        # name
        if self._hw:
            hw_node.add(s['attr_course_homeworks_homework_name'],
                self._hw['name'], 'name')
        else:
            hw_node.add(s['attr_course_homeworks_homework_name'],
                hw_show_name, hw_show_path)

        # description
        if self._hw and \
            not hw_show_description_cannot_be_decoded_with_default_encoding:
            hw_node.add(s['attr_course_homeworks_homework_description'],
                self._hw['description'], 'description')
        else:
            hw_node.add(s['attr_course_homeworks_homework_description'],
                hw_show_description, hw_show_path)

        # download_file_path
        if self._hw:
            hw_node.add(s['attr_course_homeworks_homework_download_file_path'],
                self._hw['file_path'], 'file_path')
        else:
            hw_node.add(s['attr_course_homeworks_homework_download_file_path'],
                hw_show_download_file_path, hw_show_path)

        # url
        if self._hw:
            hw_node.add(s['attr_course_homeworks_homework_url'],
                self._hw['url'], 'url')
        else:
            hw_node.add(s['attr_course_homeworks_homework_url'],
                hw_show_url, hw_show_path)

        # type
        hw_node.add(s['attr_course_homeworks_homework_type'],
            hw_show_type, hw_show_path)

        # method
        hw_node.add(s['attr_course_homeworks_homework_method'],
            hw_show_method, hw_show_path)

        # percentage
        hw_node.add(s['attr_course_homeworks_homework_percentage'],
            hw_show_percentage, hw_show_path)

        # pub
        if self._hw:
            hw_node.add(s['attr_course_homeworks_homework_pub'],
                make_date_hour(self._hw['pub_date'], self._hw['pub_hour']),
                    ['pub_date', 'pub_hour'])

        # end
        if self._hw:
            hw_node.add(s['attr_course_homeworks_homework_end'],
                make_date_hour(self._hw['end_date'], self._hw['end_hour']),
                    ['end_date', 'end_hour'])
        else:
            hw_node.add(s['attr_course_homeworks_homework_end'],
                hw_show_end, hw_show_path)

        # is_subm
        if self._hw:
            hw_node.add(s['attr_course_homeworks_homework_is_subm'],
                hw_show_is_subm, 'is_subm')
        else:
            hw_node.add(s['attr_course_homeworks_homework_is_subm'],
                hw_show_is_subm, hw_show_path)

        # hand_time
        if hw_show_hand_time != None:
            hw_node.add(s['attr_course_homeworks_homework_hand_time'],
                hw_show_hand_time, hw_show_path)

        # submitted_file_path
        if hw_show_submitted_file_path != None:
            hw_node.add(s['attr_course_homeworks_homework_submitted_file_path'],
                hw_show_submitted_file_path, hw_show_path)

        hw_node.finish()
        self.add(s['file_course_homeworks_homework'], hw_node)

        # 有公佈作業評語的才會有這項，因此下載作業檔案時我們並不依賴這裡的資訊
        if self._hw and len(self._hw['hw_scores']) > 0:
            assert set(self._hw['hw_scores'][0].keys()) == set(hw_score_keys)
            assert self._hw['hw_scores'][0]['course_sn'] == self._course_sn
            assert self._hw['hw_scores'][0]['hw_sn'] == self._hw['sn']
            hw_scores_dir = Directory(self.vfs, self)
            for index, hw_score in enumerate(self._hw['hw_scores']):
                hw_score_node = JSONFile(self.vfs, hw_scores_dir)
                hw_score_filename = '{:04}.json'.format(index + 1)
                hw_score_node.add(
                    s['attr_course_homeworks_homework_hand_time'],
                    hw_score['hand_time'], 'hand_time')
                hw_score_node.add(
                    s['attr_course_homeworks_homework_submitted_file_path'],
                    hw_score['file_path'], 'file_path')
                hw_score_node.add(
                    s['attr_course_homeworks_homework_sw'],
                    hw_score['sw'], 'sw')
                hw_score_node.add(
                    s['attr_course_homeworks_homework_ranking_grade'],
                    hw_score['ranking_grade'], 'ranking_grade')
                hw_score_node.add(
                    s['attr_course_homeworks_homework_score'],
                    hw_score['score'], 'score')
                hw_score_node.add(
                    s['attr_course_homeworks_homework_evaluation'],
                    hw_score['evaluation'], 'evaluation')
                hw_score_node.finish()
                hw_scores_dir.add(hw_score_filename, hw_score_node)
            self.add(s['dir_course_homeworks_homework_scores'],
                hw_scores_dir)
            hw_scores_dir.ready = True

        # 作業評語
        hw_eval_rows_all = hw_eval_page.xpath('//div[@id="sect_cont"]/table/tr')
        hw_eval_rows = hw_eval_rows_all[1:]
        hw_eval_header_row = hw_eval_rows_all[0]

        assert len(hw_eval_header_row) == 3
        assert hw_eval_header_row[0].text in ['學號', 'ID']
        assert hw_eval_header_row[1].text in ['成績', 'Grades']
        assert hw_eval_header_row[2].text in ['作業評語', 'Comments']

        if len(hw_eval_rows) > 0:
            hw_eval_dir = Directory(self.vfs, self)
            hw_eval_list_file = CSVFile(self.vfs, hw_eval_dir)
            hw_eval_dir.add(s['file_course_homeworks_homework_eval_table'],
                hw_eval_list_file)

            hw_eval_list_file.add([
                s['attr_course_homeworks_homework_eval_id'],
                s['attr_course_homeworks_homework_eval_grades'],
                s['attr_course_homeworks_homework_eval_comments']])

            for index, hw_eval_row in enumerate(hw_eval_rows):
                hw_eval_item_file = JSONFile(self.vfs, hw_eval_dir)

                hw_eval_row_id = element_get_text(hw_eval_row[0]).rstrip('\xa0')
                hw_eval_row_grades = element_get_text(hw_eval_row[1]).rstrip('\xa0')
                hw_eval_row_comments = element_get_text(hw_eval_row[2]).rstrip('\xa0')
                hw_eval_item_file.add(
                    s['attr_course_homeworks_homework_eval_id'],
                    hw_eval_row_id, hw_eval_path)
                hw_eval_item_file.add(
                    s['attr_course_homeworks_homework_eval_grades'],
                    hw_eval_row_grades, hw_eval_path)
                hw_eval_item_file.add(
                    s['attr_course_homeworks_homework_eval_comments'],
                    hw_eval_row_comments, hw_eval_path)

                hw_eval_item_filename = '{:04} {}.json'.format(
                    index + 1, hw_eval_row_id)
                hw_eval_item_file.finish()
                hw_eval_dir.add(hw_eval_item_filename, hw_eval_item_file)

                hw_eval_list_file.add([
                    hw_eval_row_id, hw_eval_row_grades, hw_eval_row_comments])

                if hw_eval_row_id and not hw_eval_row_id.startswith('*') and \
                    quote(hw_eval_row_id) == hw_eval_row_id:
                    hw_eval_dir.add(hw_eval_row_id, InternalLink(
                        self.vfs, hw_eval_dir, self.vfs.root.students.add_student(
                            hw_eval_row_id, sn=self._course_sn, pwd=hw_eval_dir)))

            hw_eval_list_file.finish()
            self.add(s['dir_course_homeworks_homework_eval'], hw_eval_dir)
            hw_eval_dir.ready = True

        # 相關檔案
        if hw_show_download_file_path:
            download_files_dir = Directory(self.vfs, self)
            download_filename = hw_show_download_file_path
            download_path = '/course/{}/hw/{}'.format(
                self._course_sn, download_filename)
            download_files_dir.add(download_filename, DownloadFile(
                self.vfs, download_files_dir, download_path))
            download_files_dir.ready = True
            self.add(s['dir_course_homeworks_homework_download_files'],
                download_files_dir)

        # 已上傳檔案
        if need_submitted_files_dir:
            submitted_files_dir = Directory(self.vfs, self)
            submitted_filename = hw_show_submitted_file_path
            submitted_path, submitted_args = url_to_path_and_args(
                hw_show_submitted_file_path_element[0].get('href'))
            submitted_files_dir.add(submitted_filename, StateDownloadFile(
                self.vfs, submitted_files_dir, submitted_path, args=submitted_args,
                    steps=[(frame_path, frame_args), (hw_list_path, {})]))
            submitted_files_dir.ready = True
            self.add(s['dir_course_homeworks_homework_submitted_files'],
                submitted_files_dir)

        # 作業觀摩
        hw_view_content = hw_view_page.xpath('//div[@id="sect_cont"]')[0]
        if len(hw_view_content) > 1:
            assert len(hw_view_content) == 4
            assert hw_view_content[0].tag == 'p'
            assert not hw_view_content[0].text
            assert hw_view_content[2].tag == 'table'
            assert hw_view_content[2][0].tag == 'caption'
            assert hw_view_content[2][1].tag == 'tr'
            assert hw_view_content[2][1][0].tag == 'th'
            assert hw_view_content[2][1][0].text in ['姓名', 'Name']
            assert hw_view_content[2][1][1].tag == 'th'
            assert hw_view_content[2][1][1].text in ['作業區', 'Assignments']
            hw_view_table = hw_view_content[2][2:]
            great_dir = Directory(self.vfs, self)
            for row in hw_view_table:
                assert len(row) == 2
                assert row[0].tag == 'td'
                assert row[1].tag == 'td'
                assert len(row[1]) == 1
                assert row[1][0].tag == 'a'
                student_name = row[0].text.strip()

                # 借用 Python parser 來讀 JavaScript code
                student_hw_script = row[1][0].get('onclick')
                student_hw_link = js_window_open_get_url(student_hw_script)

                # 從網址的 query string 找出學號
                student_hw_path, student_hw_args = \
                    url_to_path_and_args(student_hw_link)
                student_id = student_hw_args['hw_sn_sw']
                assert student_hw_args['csn'] == self._course_sn
                assert student_hw_args['hw_sn'] == self._hw_sn

                student_hw_filename = row[1][0].text

                student_dirname = '{} {}'.format(student_id, student_name)
                student_dir = Directory(self.vfs, great_dir)

                student_info_node = JSONFile(self.vfs, student_dir)
                student_info_node.add(
                    s['attr_course_homeworks_homework_great_assignments_id'],
                    student_id, hw_view_path)
                student_info_node.add(
                    s['attr_course_homeworks_homework_great_assignments_name'],
                    student_name, hw_view_path)
                student_info_node.finish()
                student_dir.add(
                    s['file_course_homeworks_homework_great_assignments_info'],
                    student_info_node)

                if student_hw_filename:
                    student_assignment_dir = Directory(self.vfs, student_dir)
                    student_assignment_dir.add(student_hw_filename,
                        StateDownloadFile(self.vfs, student_assignment_dir,
                            student_hw_path, args=student_hw_args, steps=[
                                (frame_path, frame_args), (hw_list_path, {})]))
                    student_assignment_dir.ready = True
                    student_dir.add(
                        s['dir_course_homeworks_homework_great_assignments_assignment'],
                        student_assignment_dir)

                student_dir.add(student_id, InternalLink(self.vfs, student_dir,
                    self.vfs.root.students.add_student(
                        student_id, sn=self._course_sn, pwd=student_dir)))

                student_dir.ready = True
                great_dir.add(student_dirname, student_dir)

            great_dir.ready = True
            self.add(s['dir_course_homeworks_homework_great_assignments'],
                great_dir)
        else:
            assert len(hw_view_content) == 1
            assert hw_view_content[0].tag == 'p'
            assert hw_view_content[0].text in ['目前無作業觀摩', 'No Great Assignment']

        self.ready = True

class CourseGradesDirectory(Directory):
    def __init__(self, vfs, parent, course_sn, grades):
        super().__init__(vfs, parent)
        self._course_sn = course_sn
        self._grades = grades

    def fetch(self):
        s = self.vfs.strings

        grade_keys = ['main_sn', 'course_sn', 'tier', 'item', 'percent', 'sub',
            'grade_isranking', 'notes', 'show', 'is_changed']
        optional_grade_keys = ['grade', 'evaluation']
        sub_keys = ['sub_sn', 'main_sn', 'course_sn', 'item', 'percent',
            'grade_isranking', 'notes', 'show', 'is_changed']
        optional_sub_keys = ['grade', 'evaluation']

        # 課程主頁面
        frame_path = '/modules/index.php'
        frame_args = {'csn': self._course_sn, 'default_fun': 'grade'}

        # 成績列表
        grade_path = '/modules/grade/grade.php'
        grade_args = {}

        # 重複爬網頁直到頁面中沒有隱藏項目為止
        self.vfs.request.web(frame_path, args=frame_args)
        while True:
            grade_page = self.vfs.request.web(grade_path, args=grade_args)
            grade_page_has_hidden_rows = False
            for a in grade_page.xpath('//div[@id="sect_cont"]/table/tr/td/a'):
                path, args = url_to_path_and_args(a.get('href'))
                if 'op' in args and args['op'] == 'stu_sub':
                    grade_args = args
                    grade_page_has_hidden_rows = True
                    break
            if not grade_page_has_hidden_rows:
                break

        assert len(grade_page.xpath('//table')) > 0

        grade_rows_all = grade_page.xpath('//div[@id="sect_cont"]/table[1]/tr')
        grade_rows = grade_rows_all[1:]
        grade_header_row = grade_rows_all[0]

        assert len(grade_header_row) == 8
        assert grade_header_row[0].text in ['項目', 'Item']
        assert grade_header_row[1].text in ['比重', 'Weight']
        assert grade_header_row[2].text in ['子項目', 'Sub-item']
        assert grade_header_row[3].text in ['評分方式', 'Grading System']
        assert grade_header_row[4].text in ['說明', 'Description']
        assert grade_header_row[5].text in ['得分', 'Grade']
        assert grade_header_row[6].text in ['評語', 'Comments']
        assert grade_header_row[7].text in ['成績公布', 'Show Grades']

        grade_row_count = len(grade_rows)

        # 把學期成績和出缺席紀錄移動到最後面
        if len(self._grades) > 0:
            attendance_grades = list()
            semester_grades = list()
            for grade in self._grades:
                if grade['tier'] == '0':
                    attendance_grades.append(grade)
                elif grade['tier'] == '3':
                    semester_grades.append(grade)
            for attendance_grade in attendance_grades:
                self._grades.remove(attendance_grade)
                self._grades.append(attendance_grade)
            for semester_grade in semester_grades:
                self._grades.remove(semester_grade)
                self._grades.append(semester_grade)
            grade_count = len(self._grades)
            for grade in self._grades:
                grade_count += len(grade['sub'])
            assert grade_count == grade_row_count

        grade_row_index = 0
        grade_index = (0, -1)

        grade_list_file = CSVFile(self.vfs, self)
        self.add(s['file_course_grades_table'], grade_list_file)

        grade_list_file.add([
            s['attr_course_grades_main_sn'],
            s['attr_course_grades_sub_sn'],
            s['attr_course_grades_tier'],
            s['attr_course_grades_item'],
            s['attr_course_grades_percent'],
            s['attr_course_grades_grade_isranking'],
            s['attr_course_grades_notes'],
            s['attr_course_grades_grade'],
            s['attr_course_grades_evaluation'],
            s['attr_course_grades_show'],
            s['attr_course_grades_is_changed']])

        while grade_row_index < grade_row_count:
            grade_item_file = JSONFile(self.vfs, self)
            grade_item_filename = '{:02}'.format(grade_row_index + 1)

            grade_row = grade_rows[grade_row_index]

            if grade_row.get('class') == 'sub':
                grade_row_tier = ''
            elif grade_row.get('class') == 'sem':
                grade_row_tier = '3'
            else:
                grade_row_tier = None

            # 項目
            if grade_row_tier == '':
                grade_row_item = element_get_text(grade_row[0]).lstrip('　')
            else:
                grade_row_item = element_get_text(grade_row[0])

            # 比重
            grade_row_percent = element_get_text(grade_row[1]).rstrip('%')

            # 子項目
            if grade_row[2].text in ['無', 'None']:
                assert len(grade_row[2]) == 0
            elif grade_row_tier == None:
                assert len(grade_row[2]) == 1
                assert grade_row[2][0].tag == 'a'
                assert grade_row[2][0].text in ['隱藏', 'Hide']
                grade_row_tier = '2'

            # 評分方式
            if grade_row[3].text in ['百分制', 'Number Grades']:
                grade_row_grade_isranking = '0'
            elif grade_row[3].text in ['等第制', 'Letter Grades']:
                grade_row_grade_isranking = '1'
            elif grade_row[3].text == None:
                grade_row_grade_isranking = ''
            else:
                assert False

            # 說明
            assert len(grade_row[4]) == 1
            assert grade_row[4][0].tag == 'p'
            grade_row_notes = element_get_text(grade_row[4][0])

            # 得分
            if len(grade_row[5]) >= 1:
                assert len(grade_row[5]) == 1
                assert grade_row[5][0].tag == 'font'
                grade_row_grade = element_get_text(grade_row[5][0])
            else:
                grade_row_grade = element_get_text(grade_row[5])

            # 評語
            if grade_row_tier == '' or grade_row_tier == '3':
                assert len(grade_row[6]) == 0
                grade_row_evaluation = element_get_text(grade_row[6])
            elif grade_row_tier == None and len(grade_row[6]) == 0:
                grade_row_evaluation = element_get_text(grade_row[6])
                grade_row_tier = '0'
            else:
                assert len(grade_row[6]) == 1
                assert grade_row[6][0].tag == 'p'
                grade_row_evaluation = element_get_text(grade_row[6][0])
                if grade_row_tier != '2':
                    grade_row_tier = '1'

            # 成績公布
            if grade_row[7].text in ['不公布', 'No']:
                grade_row_show = 'N'
            elif grade_row[7].text in ['公布個人', 'Individual']:
                grade_row_show = 'P'
            else:
                assert False

            # 移動到下一列
            grade_row_index += 1

            if len(self._grades) > 0:
                grade = self._grades[grade_index[0]]
                if grade_index[1] >= 0:
                    grade = grade['sub'][grade_index[1]]

                if grade_index[1] < 0:
                    assert set(grade.keys()) - set(optional_grade_keys) == set(grade_keys)
                    assert grade['tier'] in ['0', '1', '2', '3']
                else:
                    assert set(grade.keys()) - set(optional_sub_keys) == set(sub_keys)
                assert grade['grade_isranking'] in ['0', '1']
                assert grade['show'] in ['N', 'P']
                assert grade['is_changed'] in ['0', '1']

                grade_item_filename += ' {:08}'.format(int(grade['main_sn']))
                if grade_index[1] >= 0:
                    grade_item_filename += '-{:08}'.format(int(grade['sub_sn']))

                assert grade['course_sn'] == self._course_sn
                if grade_index[1] >= 0:
                    assert grade['main_sn'] == self._grades[grade_index[0]]['main_sn']

                if 'tier' in grade:
                    assert grade['tier'] == grade_row_tier
                else:
                    assert grade_row_tier == ''

                assert grade['item'] == grade_row_item
                assert grade['percent'] == grade_row_percent

                # 已知 grade_isranking 的值很可能不正確，因此這裡直接忽略
                # assert grade['grade_isranking'] == grade_row_grade_isranking

                assert grade['notes'] == grade_row_notes, \
                    (grade['notes'], grade_row_notes)

                # 等第制評分的項目通常找不到這項，所以沒有 else 的 assert
                if 'grade' in grade:
                    assert grade['grade'] == grade_row_grade

                if 'evaluation' in grade:
                    assert grade['evaluation'] == grade_row_evaluation
                else:
                    grade_row_evaluation == ''

                assert grade['show'] == grade_row_show

                # 移動到下一筆資料
                if grade_index[1] >= 0:
                    sub_count = len(self._grades[grade_index[0]]['sub'])
                    if grade_index[1] + 1 >= sub_count:
                        grade_index = (grade_index[0] + 1, -1)
                    else:
                        grade_index = (grade_index[0], grade_index[1] + 1)
                else:
                    if len(self._grades[grade_index[0]]['sub']) > 0:
                        grade_index = (grade_index[0], grade_index[1] + 1)
                    else:
                        grade_index = (grade_index[0] + 1, -1)
            else:
                grade = None

            grade_list_file_row = list()

            # main_sn
            if grade:
                grade_item_file.add(s['attr_course_grades_main_sn'],
                    grade['main_sn'], 'main_sn')
                grade_list_file_row.append(grade['main_sn'])
            else:
                grade_list_file_row.append('')

            # sub_sn
            if grade and 'sub_sn' in grade:
                grade_item_file.add(s['attr_course_grades_sub_sn'],
                    grade['sub_sn'], 'sub_sn')
                grade_list_file_row.append(grade['sub_sn'])
            else:
                grade_list_file_row.append('')

            # tier
            if grade_row_tier == '':
                tier = s['value_course_grades_tier_']
            elif grade_row_tier == '0':
                tier = s['value_course_grades_tier_0']
            elif grade_row_tier == '1':
                tier = s['value_course_grades_tier_1']
            elif grade_row_tier == '2':
                tier = s['value_course_grades_tier_2']
            elif grade_row_tier == '3':
                tier = s['value_course_grades_tier_3']
            else:
                assert False

            if grade:
                grade_item_file.add(s['attr_course_grades_tier'], tier, 'tier')
            else:
                grade_item_file.add(s['attr_course_grades_tier'], tier, grade_path)
            grade_list_file_row.append(tier)

            # item
            if grade:
                grade_item_file.add(s['attr_course_grades_item'],
                    grade['item'], 'item')
            else:
                grade_item_file.add(s['attr_course_grades_item'],
                    grade_row_item, grade_path)
            if grade_row_tier == '':
                grade_list_file_row.append('　' + grade_row_item)
            else:
                grade_list_file_row.append(grade_row_item)

            # percent
            percent = grade_row_percent + '%'
            if grade:
                grade_item_file.add(s['attr_course_grades_percent'],
                    percent, 'percent')
            else:
                grade_item_file.add(s['attr_course_grades_percent'],
                    percent, grade_path)
            grade_list_file_row.append(percent)

            # grade_isranking
            if grade_row_grade_isranking == '':
                grade_isranking = s['value_course_grades_grade_isranking_']
            elif grade_row_grade_isranking == '0':
                grade_isranking = s['value_course_grades_grade_isranking_0']
            elif grade_row_grade_isranking == '1':
                grade_isranking = s['value_course_grades_grade_isranking_1']
            else:
                assert False

            grade_item_file.add(s['attr_course_grades_grade_isranking'],
                grade_isranking, grade_path)
            grade_list_file_row.append(grade_isranking)

            # notes
            if grade:
                grade_item_file.add(s['attr_course_grades_notes'],
                    grade['notes'], 'notes')
            else:
                grade_item_file.add(s['attr_course_grades_notes'],
                    grade_row_notes, grade_path)
            grade_list_file_row.append(grade_row_notes)

            # grade
            if grade and 'grade' in grade:
                grade_item_file.add(s['attr_course_grades_grade'],
                    grade['grade'], 'grade')
            else:
                grade_item_file.add(s['attr_course_grades_grade'],
                    grade_row_grade, grade_path)
            grade_list_file_row.append(grade_row_grade)

            # evaluation
            if grade:
                if 'evaluation' in grade:
                    grade_item_file.add(s['attr_course_grades_evaluation'],
                        grade['evaluation'], 'evaluation')
            else:
                grade_item_file.add(s['attr_course_grades_evaluation'],
                    grade_row_evaluation, grade_path)
            grade_list_file_row.append(grade_row_evaluation)

            # show
            if grade_row_show == 'N':
                show = s['value_course_grades_show_n']
            elif grade_row_show == 'P':
                show = s['value_course_grades_show_p']
            else:
                assert False

            if grade:
                grade_item_file.add(s['attr_course_grades_show'], show, 'show')
            else:
                grade_item_file.add(s['attr_course_grades_show'], show, grade_path)
            grade_list_file_row.append(show)

            # is_changed
            if grade:
                grade_item_file.add(s['attr_course_grades_is_changed'],
                    grade['is_changed'], 'is_changed')
                grade_list_file_row.append(grade['is_changed'])
            else:
                grade_list_file_row.append('')

            grade_item_file.finish()
            grade_item_filename += ' {}.json'.format(
                grade_row_item.strip().rstrip('.'))
            self.add(grade_item_filename, grade_item_file)

            grade_list_file.add(grade_list_file_row)

        grade_list_file.finish()
        self.ready = True

class CourseShareDirectory(Directory):
    def __init__(self, vfs, parent, course_sn):
        super().__init__(vfs, parent)
        self._course_sn = course_sn

    def fetch(self):
        from enum import Enum
        s = self.vfs.strings

        frame_path = '/modules/index.php'
        frame_args = {'csn': self._course_sn, 'default_fun': 'share'}

        self.vfs.request.web(frame_path, args=frame_args)

        class AttrType(Enum):
            EMAIL = 1
            TYPE = 2
            TITLE = 3
            LINK = 4
            STRING = 5
            YEAR_MONTH = 6
            YEAR_MONTH_DATE = 7
            ENUM = 8
            RATING = 9

        share_list_attrs = [
            ['名稱', 'Name'],
            ['簡介', 'Description'],
            ['分享者', 'Author'],
            ['評分', 'Rating'],
            ['點閱數', 'Views'],
        ]

        share_type_attrs = [
            ('/modules/share/share.php', {'op': 'url'},
             '/modules/share/share_url_show.php',
             s['dir_course_share_url'], [
                (['姓名', 'Name'], AttrType.EMAIL),
                (['分享類別', 'Type'], AttrType.TYPE,
                    ['網頁介紹', 'Website Introduction']),
                (['網站名稱', 'Website'], AttrType.TITLE),
                (['網址', 'URL'], AttrType.LINK,
                    s['attr_course_share_url_url']),
                (['網站介紹', 'Description'], AttrType.STRING,
                    s['attr_course_share_url_description']),
                (['評分', 'Rating'], AttrType.RATING,
                    s['attr_course_share_rating_detail']),
             ]),
            ('/modules/share/share.php', {'op': 'book'},
             '/modules/share/share_book_show.php',
             s['dir_course_share_book'], [
                (['姓名', 'Name'], AttrType.EMAIL),
                (['分享類別', 'Type'], AttrType.TYPE,
                    ['書籍介紹', 'Books Introduction']),
                (['語言', 'Language'], AttrType.STRING,
                    s['attr_course_share_book_language']),
                (['書名', 'Title'], AttrType.TITLE),
                (['版本', 'Edition'], AttrType.STRING,
                    s['attr_course_share_book_edition']),
                (['作者', 'Author'], AttrType.STRING,
                    s['attr_course_share_book_author']),
                (['出版社', 'Publisher'], AttrType.STRING,
                    s['attr_course_share_book_publisher']),
                (['出版年月', 'Published Date'], AttrType.YEAR_MONTH,
                    (s['attr_course_share_book_published_date_year'],
                     s['attr_course_share_book_published_date_month']),
                    (['年', 'year'], ['月', 'month'])),
                (['書籍介紹', 'Books Introduction'], AttrType.STRING,
                    s['attr_course_share_book_books_introduction']),
                (['書籍介紹', 'Books Introduction'], AttrType.RATING,
                    s['attr_course_share_rating_detail']),
             ]),
            ('/modules/share/share.php', {'op': 'perd'},
             '/modules/share/share_periodical_show.php',
             s['dir_course_share_perd'], [
                (['姓名', 'Name'], AttrType.EMAIL),
                (['分享類別', 'Type'], AttrType.TYPE,
                    ['文章介紹', 'Articles Introduction']),
                (['文章名稱', 'Article Title'], AttrType.TITLE),
                (['期刊名稱', 'Periodical Title'], AttrType.STRING,
                    s['attr_course_share_perd_periodical_title']),
                (['作者', 'Author'], AttrType.STRING,
                    s['attr_course_share_perd_author']),
                (['出版社', 'Publisher'], AttrType.STRING,
                    s['attr_course_share_perd_publisher']),
                (['出版年月', 'Published Date'], AttrType.YEAR_MONTH_DATE,
                    (s['attr_course_share_perd_published_date_year'],
                     s['attr_course_share_perd_published_date_month'],
                     s['attr_course_share_perd_published_date_date']),
                    (['年'], ['月'], ['日'])),
                (['發刊週期', 'Frequency'], AttrType.ENUM,
                    s['attr_course_share_perd_frequency'], [
                    ('1', ['週刊', 'weekly'],
                        s['value_course_share_perd_frequency_1']),
                    ('2', ['雙週刊', 'biweekly'],
                        s['value_course_share_perd_frequency_2']),
                    ('3', ['月刊', 'monthly'],
                        s['value_course_share_perd_frequency_3']),
                    ('4', ['季刊', 'quarterly'],
                        s['value_course_share_perd_frequency_4']),
                    ('5', ['年刊', 'annual'],
                        s['value_course_share_perd_frequency_5']),
                    ('6', ['其他', 'other'],
                        s['value_course_share_perd_frequency_6'])]),
                (['文章介紹', 'Articles Introduction'], AttrType.STRING,
                    s['attr_course_share_perd_articles_introduction']),
                (['文章介紹', 'Articles Introduction'], AttrType.RATING,
                    s['attr_course_share_rating_detail']),
            ]),
        ]

        for share_type_attr in share_type_attrs:
            # 首先我們要取得這類型資源分享的清單，但因為「簡介」欄位的內容可能
            # 會在不正確的地方被截斷，導致編碼錯誤，使得網頁沒有被完整讀入。因
            # 這裡我們改用手動解碼來避免資料遺失。
            assert len(share_type_attr) == 5
            share_list_path = share_type_attr[0]
            share_list_args = share_type_attr[1]
            share_list_html = BytesIO()
            self.vfs.request.file(
                share_list_path, share_list_html, args=share_list_args)

            share_list_page = etree.fromstring(
                share_list_html.getvalue().decode('big5', errors='replace'),
                etree.HTMLParser(remove_comments=True))
            share_list_tables = share_list_page.xpath(
                '//div[@id="sect_cont"]//table[1]')

            if len(share_list_tables) == 2:
                # 這表示有遇到編碼錯誤，第二項的資料才是完整的
                share_list_table = share_list_tables[1]
            elif len(share_list_tables) == 1:
                # 只有一項是正常現象
                share_list_table = share_list_tables[0]
            else:
                assert False

            share_list_rows_all = share_list_table.xpath('./tr')
            share_list_rows = share_list_rows_all[1:]
            share_list_header_row = share_list_rows_all[0]

            assert len(share_list_header_row) == len(share_list_attrs)
            for index, share_list_attr in enumerate(share_list_attrs):
                assert share_list_header_row[index].tag == 'th'
                assert share_list_header_row[index].text in share_list_attr

            # 沒有資料就算了，直接換下一種類型
            if len(share_list_rows) == 0:
                continue

            share_show_path = share_type_attr[2]
            share_list_dirname = share_type_attr[3]
            share_show_fields = share_type_attr[4]

            collected_accounts = OrderedDict()
            share_list_dir = Directory(self.vfs, self)

            for share_list_row in share_list_rows:
                # 名稱
                if len(share_list_row[0]) == 1:
                    assert not share_list_row[0].text
                    assert share_list_row[0][0].tag == 'a'
                    share_name = element_get_text(share_list_row[0][0]) \
                        .replace('∼', '～').replace('•', '‧')
                elif len(share_list_row[0]) == 0:
                    share_name = element_get_text(share_list_row[0]) \
                        .replace('∼', '～').replace('•', '‧')
                else:
                    assert False

                # 簡介
                share_list_more_element = share_list_row[1].xpath('.//a')
                assert len(share_list_more_element) == 1
                assert share_list_more_element[0].tag == 'a'
                assert share_list_more_element[0].get('href')
                assert len(share_list_more_element[0]) == 1
                assert share_list_more_element[0][0].tag == 'span'
                assert share_list_more_element[0][0].get('class') == 'more'
                assert share_list_more_element[0][0].text == 'more »'
                path, args = url_to_path_and_args(
                    share_list_more_element[0].get('href'))

                assert share_show_path.rsplit('/', maxsplit=1)[1] == path
                share_sn = args['sn']
                share_show_args = {'sn': share_sn}

                share_file = JSONFile(self.vfs, share_list_dir)
                share_filename = format_filename(share_sn, share_name, 'json')

                share_file.add(s['attr_course_share_sn'],
                    share_sn, share_list_path)
                share_file.add(s['attr_course_share_name'],
                    share_name, share_list_path)

                # 分享者
                assert len(share_list_row[2]) == 1
                assert share_list_row[2][0].text
                assert share_list_row[2][0].tag == 'a'
                assert share_list_row[2][0].get('href').startswith('mailto:')

                share_shared_by = share_list_row[2][0].text
                share_shared_by_email = share_list_row[2][0].get('href')[7:]

                share_file.add(s['attr_course_share_author'],
                    share_shared_by, share_list_path)
                share_file.add(s['attr_course_share_author_email'],
                    share_shared_by_email, share_list_path)

                if share_shared_by_email.endswith('@ntu.edu.tw') or \
                    share_shared_by_email.endswith('@csie.ntu.edu.tw'):
                    collected_accounts[share_shared_by_email.split('@')[0]] = None

                # 評分
                assert len(share_list_row[3]) == 0
                share_rating = element_get_text(share_list_row[3])
                share_file.add(s['attr_course_share_rating'],
                    share_rating, share_list_path)

                # 點閱數
                assert len(share_list_row[4]) == 0
                share_views = element_get_text(share_list_row[4])
                share_file.add(s['attr_course_share_views'],
                    share_views, share_list_path)

                # 下載分享詳細資料
                share_show_page = self.vfs.request.web(
                    share_show_path, args=share_show_args)
                share_show_rows = share_show_page.xpath(
                    '//div[@id="sect_cont"]/table/tr')
                assert len(share_show_rows) == len(share_show_fields)

                for index, share_show_row in enumerate(share_show_rows):
                    share_show_field = share_show_fields[index]
                    field_name = share_show_field[0]
                    field_type = share_show_field[1]

                    if field_type == AttrType.EMAIL:
                        assert len(share_show_field) == 2
                        element = row_get_value(share_show_row,
                            field_name, {}, free_form=True, return_object=True)
                        assert len(element) == 1
                        assert element[0].text
                        assert element[0].tag == 'a'
                        assert element[0].get('href').startswith('mailto:')
                        assert element[0].text == share_shared_by
                        assert element[0].get('href')[7:] == share_shared_by_email

                    elif field_type == AttrType.TYPE:
                        assert len(share_show_field) == 3
                        field_acceptable_values = share_show_field[2]
                        row_get_value(share_show_row, field_name,
                            { tuple(field_acceptable_values): None })

                    elif field_type == AttrType.TITLE:
                        assert len(share_show_field) == 2
                        title = row_get_value(share_show_row,
                            field_name, {}, free_form=True)

                        # 如果不一樣，表示 CEIBA 又因為沒有跳脫特殊字元，
                        # 導致資料出錯了
                        if title != share_name:
                            assert ''.join(etree.fromstring(
                                title, etree.HTMLParser()).itertext()) == \
                                share_name
                            # 用正確的資料覆蓋掉前面的錯誤資料
                            share_filename = format_filename(
                                share_sn, title, 'json')
                            share_file.replace(s['attr_course_share_name'],
                                title, share_show_path)

                    elif field_type == AttrType.LINK:
                        assert len(share_show_field) == 3
                        field_output = share_show_field[2]
                        element = row_get_value(share_show_row, field_name,
                            {}, free_form=True, return_object=True)
                        assert len(element) == 1
                        assert element[0].tag == 'a'
                        assert element[0].text == element[0].get('href')
                        share_file.add(field_output,
                            element[0].get('href'), share_show_path)

                    elif field_type == AttrType.STRING:
                        assert len(share_show_field) == 3
                        field_output = share_show_field[2]
                        element = row_get_value(share_show_row, field_name,
                            {}, free_form=True, return_object=True)

                        # 這裡不檢查是否只有使用到 br 標籤，因為 CEIBA 沒有跳脫
                        # 特殊字元，使用者可以塞各種不認識的標籤進來……
                        string = ''.join(element.itertext())
                        if string.find('\r') >= 0 and string.find('\n') < 0:
                            string = string.replace('\r', '\n')
                        share_file.add(field_output, string, share_show_path)

                    elif field_type == AttrType.YEAR_MONTH:
                        assert len(share_show_field) == 4
                        assert len(share_show_field[2]) == 2
                        assert len(share_show_field[3]) == 2
                        field_output_year = share_show_field[2][0]
                        field_output_month = share_show_field[2][1]
                        field_separator_year = share_show_field[3][0]
                        field_separator_month = share_show_field[3][1]

                        year_month = row_get_value(share_show_row,
                            field_name, {}, free_form=True)

                        for possible_separator in field_separator_year:
                            parts = year_month.split(possible_separator)
                            if len(parts) == 2:
                                year, remaining = parts
                                year = year.strip()
                                break
                            assert len(parts) == 1
                        else:
                            assert False

                        for possible_separator in field_separator_month:
                            parts = remaining.split(possible_separator)
                            if len(parts) == 2:
                                month = parts[0].strip()
                                break
                            assert len(parts) == 1
                        else:
                            assert False

                        share_file.add(field_output_year, year, share_show_path)
                        share_file.add(field_output_month, month, share_show_path)

                    elif field_type == AttrType.YEAR_MONTH_DATE:
                        assert len(share_show_field) == 4
                        assert len(share_show_field[2]) == 3
                        assert len(share_show_field[3]) == 3
                        field_output_year = share_show_field[2][0]
                        field_output_month = share_show_field[2][1]
                        field_output_date = share_show_field[2][2]
                        field_separator_year = share_show_field[3][0]
                        field_separator_month = share_show_field[3][1]
                        field_separator_date = share_show_field[3][2]

                        year_month_date = row_get_value(share_show_row,
                            field_name, {}, free_form=True)

                        for possible_separator in field_separator_year:
                            parts = year_month_date.split(possible_separator)
                            if len(parts) == 2:
                                year, remaining1 = parts
                                year = year.strip()
                                break
                            assert len(parts) == 1
                        else:
                            assert False

                        for possible_separator in field_separator_month:
                            parts = remaining1.split(possible_separator)
                            if len(parts) == 2:
                                month, remaining2 = parts
                                month = month.strip()
                                break
                            assert len(parts) == 1
                        else:
                            assert False

                        for possible_separator in field_separator_date:
                            parts = remaining2.split(possible_separator)
                            if len(parts) == 2:
                                date = parts[0].strip()
                                break
                            assert len(parts) == 1
                        else:
                            assert False

                        share_file.add(field_output_year, year, share_show_path)
                        share_file.add(field_output_month, month, share_show_path)
                        share_file.add(field_output_date, date, share_show_path)

                    elif field_type == AttrType.ENUM:
                        assert len(share_show_field) == 4
                        field_output = share_show_field[2]
                        field_values = share_show_field[3]

                        value_mappings = dict()
                        for field_value in field_values:
                            value_mappings[tuple(field_value[1])] = field_value[2]

                        enum_value = row_get_value(share_show_row, field_name,
                            value_mappings)

                        share_file.add(field_output, enum_value, share_show_path)

                    elif field_type == AttrType.RATING:
                        assert len(share_show_field) == 3
                        field_output = share_show_field[2]
                        element = row_get_value(share_show_row, field_name,
                            {}, free_form=True, return_object=True)
                        assert len(element) == 2
                        assert element[1].tag == 'div'
                        assert element[0].tag == 'p'
                        assert element[0].get('class') == 'rate'
                        rating = list(element[0].itertext())[0].strip()
                        assert rating.startswith('平均得分：') or \
                            rating.startswith('Average：')
                        share_file.add(field_output, rating, share_show_path)

                    else:
                        assert False

                share_file.finish()
                share_list_dir.add(share_filename, share_file)

            for account in collected_accounts.keys():
                if self.vfs.root.teachers.is_teacher(account):
                    share_list_dir.add(account, InternalLink(self.vfs,
                        share_list_dir, self.vfs.root.teachers.add_teacher(
                            account, pwd=share_list_dir)))
                else:
                    share_list_dir.add(account, InternalLink(self.vfs,
                        share_list_dir, self.vfs.root.students.add_student(
                            account, sn=self._course_sn, pwd=share_list_dir)))

            self.add(share_list_dirname, share_list_dir)
            share_list_dir.ready = True

        self.ready = True

class CourseVoteDirectory(Directory):
    def __init__(self, vfs, parent, course_sn):
        super().__init__(vfs, parent)
        self._course_sn = course_sn

    def fetch(self):
        s = self.vfs.strings

        frame_path = '/modules/index.php'
        frame_args = {'csn': self._course_sn, 'default_fun': 'vote'}

        vote_list_path = '/modules/vote/vote.php'

        self.vfs.request.web(frame_path, args=frame_args)
        vote_list_page = self.vfs.request.web(vote_list_path)

        vote_list_rows_all = vote_list_page.xpath('//div[@id="sect_cont"]/table/tr')
        vote_list_rows = vote_list_rows_all[1:]
        vote_list_header_row = vote_list_rows_all[0]

        assert len(vote_list_header_row) == 5
        assert vote_list_header_row[0].text in ['公告日期', 'Date']
        assert vote_list_header_row[1].text in ['投票主題', 'Vote Topic']
        assert vote_list_header_row[2].text in ['開始日期', 'Start']
        assert vote_list_header_row[3].text in ['結束日期', 'End']
        assert vote_list_header_row[4].text in ['結果', 'Results']

        for vote_list_row in vote_list_rows:
            # 公告日期
            assert len(vote_list_row[0]) == 0
            vote_ann_date = element_get_text(vote_list_row[0])

            # 投票主題
            assert len(vote_list_row[1]) == 0
            vote_topic = element_get_text(vote_list_row[1]).strip()

            # 開始日期
            assert len(vote_list_row[2]) == 0
            vote_start_date = element_get_text(vote_list_row[2])

            # 結束日期
            assert len(vote_list_row[3]) == 0
            vote_end_date = element_get_text(vote_list_row[3])

            # 結果
            assert len(vote_list_row[4]) == 1
            assert vote_list_row[4][0].tag == 'a'
            assert vote_list_row[4][0].get('onclick')

            vote_result_script = vote_list_row[4][0].get('onclick')
            vote_result_link = js_window_open_get_url(vote_result_script)

            vote_result_args = url_to_path_and_args(vote_result_link)[1]
            vote_vid = vote_result_args['vid']

            vote_file = JSONFile(self.vfs, self)
            vote_filename = format_filename(vote_vid, vote_topic, 'json')

            vote_file.add(s['attr_course_vote_ann_date'],
                vote_ann_date, vote_list_path)
            vote_file.add(s['attr_course_vote_topic'],
                vote_topic, vote_list_path)
            vote_file.add(s['attr_course_vote_start_date'],
                vote_start_date, vote_list_path)
            vote_file.add(s['attr_course_vote_end_date'],
                vote_end_date, vote_list_path)

            vote_result_path = '/modules/vote/vote_result.php'
            vote_result_args = {'vid': vote_vid}
            vote_result_page = self.vfs.request.web(
                vote_result_path, args=vote_result_args)

            vote_result_rows = vote_result_page.xpath('/html/body/table/tr')
            assert len(vote_result_rows) == 3

            # 投票主題這裡又出現一次
            assert vote_topic == row_get_value(vote_result_rows[0],
                ['投票主題', 'Vote Topic'], {}, free_form=True)

            # 統計
            statistics_element = row_get_value(vote_result_rows[1],
                ['統計', 'Statistics'], {}, free_form=True, return_object=True)

            # 應該可以預期會是 2 到 4 行字，br 標籤數會比行數少一個
            assert len(statistics_element) >= 1 and \
                len(statistics_element) <= 3
            assert list(map(lambda x: x.tag, statistics_element)) == \
                ['br'] * len(statistics_element)

            statistics_text = list(statistics_element.itertext())
            statistics_text_kinds = statistics_text[:-1]
            statistics_text_all = statistics_text[-1]

            separators = [
                ((['名教師，', 'teacher(s) in total,'],
                  ['名已投，', 'voted,'],
                  ['名未投', 'not yet']),
                 (s['attr_course_vote_teachers_total'],
                  s['attr_course_vote_teachers_voted'],
                  s['attr_course_vote_teachers_not_yet'])),
                ((['名助教，', 'TA(s) in total,'],
                  ['名已投，', 'voted,'],
                  ['名未投', 'not yet']),
                 (s['attr_course_vote_tas_total'],
                  s['attr_course_vote_tas_voted'],
                  s['attr_course_vote_tas_not_yet'])),
                ((['名學生，', 'student(s) in total,'],
                  ['名已投，', 'voted,'],
                  ['名未投', 'not yet']),
                 (s['attr_course_vote_students_total'],
                  s['attr_course_vote_students_voted'],
                  s['attr_course_vote_students_not_yet']))]

            for statistics_text_kind in statistics_text_kinds:
                for possible_kind in separators:
                    if statistics_text_kind.find(possible_kind[0][0][0]) >= 0 or \
                        statistics_text_kind.find(possible_kind[0][0][1]) >= 0:
                        kind = possible_kind
                        break
                else:
                     assert False

                assert len(kind) == 2
                assert len(kind[0]) == 3
                assert len(kind[1]) == 3

                for possible_separator in kind[0][0]:
                    parts = statistics_text_kind.split(possible_separator)
                    if len(parts) == 2:
                        total, remaining1 = parts
                        total = total.strip()
                        break
                    assert len(parts) == 1
                else:
                    assert False
                vote_file.add(kind[1][0], total, vote_result_path)

                for possible_separator in kind[0][1]:
                    parts = remaining1.split(possible_separator)
                    if len(parts) == 2:
                        voted, remaining2 = parts
                        voted = voted.strip()
                        break
                    assert len(parts) == 1
                else:
                    assert False
                vote_file.add(kind[1][1], voted, vote_result_path)

                for possible_separator in kind[0][2]:
                    parts = remaining2.split(possible_separator)
                    if len(parts) == 2:
                        not_yet = parts[0].strip()
                        break
                    assert len(parts) == 1
                else:
                    assert False
                vote_file.add(kind[1][2], not_yet, vote_result_path)

                del total, voted, not_yet, remaining1, remaining2

            for possible_separator in ['票 (每人)，', 'votes for each,']:
                parts = statistics_text_all.split(possible_separator)
                if len(parts) == 2:
                    vote_votes_for_each, remaining = parts
                    vote_votes_for_each = vote_votes_for_each.strip()
                    break
                assert len(parts) == 1
            else:
                assert False
            vote_file.add(s['attr_course_vote_votes_for_each'],
                vote_votes_for_each, vote_result_path)

            for possible_separator in ['票 (總計)', 'votes in total']:
                parts = remaining.split(possible_separator)
                if len(parts) == 2:
                    vote_votes_in_total = parts[0].strip()
                    break
                assert len(parts) == 1
            else:
                assert False
            vote_file.add(s['attr_course_vote_votes_in_total'],
                vote_votes_in_total, vote_result_path)

            del remaining

            # 分佈
            distribution_element = row_get_value(vote_result_rows[2],
                ['分佈', 'Distribution'], {}, free_form=True, return_object=True)
            vote_result = list()

            for option_element in distribution_element:
                assert len(option_element) == 2
                assert option_element.tag == 'p'
                assert option_element[0].tag == 'br'
                assert option_element[1].tag == 'img'
                assert option_element[1].get('src') == '../../images/vote.jpg'
                assert option_element[1].get('width')

                option_text = list(option_element.itertext())
                assert not option_text[1].strip()

                option_name = option_text[0].strip()
                option_votes_and_percent = option_text[2].strip()

                option_votes, remaining = option_votes_and_percent.split('（')
                option_votes = option_votes.strip()

                option_percent = remaining.split('）')[0]
                assert option_percent == option_element[1].get('width')

                vote_result.append({
                    s['attr_course_vote_result_option']: option_name,
                    s['attr_course_vote_result_votes']: option_votes,
                    s['attr_course_vote_result_percent']: option_percent})

                del remaining

            vote_file.add(s['attr_course_vote_result'],
                vote_result, vote_result_path)

            vote_file.finish()
            self.add(vote_filename, vote_file)

        self.ready = True

class CourseTeacherInfoDirectory(Directory):
    def __init__(self, vfs, parent, course_sn, teacher_info):
        super().__init__(vfs, parent)
        self._course_sn = course_sn
        self._teacher_info = teacher_info

    def fetch(self):
        s = self.vfs.strings
        teacher_info_keys = ['account', 'tr_msid',
            'cname', 'ename', 'email', 'phone', 'address']
        collected_accounts = OrderedDict()

        for teacher in self._teacher_info:
            assert set(teacher.keys()) == set(teacher_info_keys)
            assert teacher['account']
            teacher_file = JSONFile(self.vfs, self)

            teacher_file.add(s['attr_course_teachers_account'],
                teacher['account'], 'account')

            if teacher['tr_msid'] == '0':
                teacher_file.add(s['attr_course_teachers_tr_msid'],
                    s['value_course_teachers_tr_msid_0'], 'tr_msid')
            elif teacher['tr_msid'] == '1':
                teacher_file.add(s['attr_course_teachers_tr_msid'],
                    s['value_course_teachers_tr_msid_1'], 'tr_msid')
            else:
                assert False

            teacher_file.add(s['attr_course_teachers_cname'],
                teacher['cname'], 'cname')
            teacher_file.add(s['attr_course_teachers_ename'],
                teacher['ename'], 'ename')
            teacher_file.add(s['attr_course_teachers_email'],
                teacher['email'], 'email')
            teacher_file.add(s['attr_course_teachers_phone'],
                teacher['phone'], 'phone')
            teacher_file.add(s['attr_course_teachers_address'],
                teacher['address'], 'address')

            teacher_file.finish()
            teacher_filename = '{}.json'.format(teacher['account'])
            self.add(teacher_filename, teacher_file)
            collected_accounts[teacher['account']] = None

        course_list_row = self.vfs.root.courses.search_course_list(self._course_sn)
        assert len(course_list_row[5]) == 1
        assert course_list_row[5][0].tag == 'a'
        assert course_list_row[5][0].get('onclick')
        teacher_script = course_list_row[5][0].get('onclick')
        teacher_link = js_window_open_get_url(teacher_script)
        collected_accounts[url_to_path_and_args(teacher_link)[1]['td']] = None

        for account in collected_accounts.keys():
            self.add(account, InternalLink(self.vfs, self,
                self.vfs.root.teachers.add_teacher(account, pwd=self)))

        self.ready = True

class CourseRosterDirectory(Directory):
    def __init__(self, vfs, parent, course_sn):
        super().__init__(vfs, parent)
        self._course_sn = course_sn

    def fetch(self):
        s = self.vfs.strings
        collected_accounts = OrderedDict()

        roster_path = '/modules/student/print.php'
        roster_args = {'course_sn': self._course_sn,
            'sort': 'student', 'current_lang': 'chinese'}
        roster_page = self.vfs.request.web(roster_path, args=roster_args)

        roster_rows_all = roster_page.xpath('//table/tr')
        roster_rows = roster_rows_all[1:]
        roster_header_row = roster_rows_all[0]

        assert len(roster_header_row) == 8
        assert roster_header_row[0].text in ['身份', 'Role']
        assert roster_header_row[1].text in ['系所', 'Department']
        assert roster_header_row[2].text in ['學號', 'ID']
        assert roster_header_row[3].text in ['姓名', 'Name']
        assert roster_header_row[4].text in ['英文姓名', 'English Name']
        assert roster_header_row[5].text in ['照片', 'Photo']
        assert roster_header_row[6].text in ['電子郵件', 'E-mail']
        assert roster_header_row[7].text in ['組別', 'Group']

        for row in roster_rows:
            student_file = JSONFile(self.vfs, self)
            student_file.add(s['attr_course_students_role'],
                element_get_text(row[0]).strip(), roster_path)
            student_file.add(s['attr_course_students_department'],
                element_get_text(row[1]).strip(), roster_path)

            student_id = element_get_text(row[2]).strip()
            student_file.add(s['attr_course_students_id'],
                student_id, roster_path)

            student_file.add(s['attr_course_students_name'],
                element_get_text(row[3]).strip(), roster_path)
            student_file.add(s['attr_course_students_english_name'],
                element_get_text(row[4]).strip(), roster_path)
            student_file.add(s['attr_course_students_email'],
                element_get_text(row[6]).strip(), roster_path)
            student_file.add(s['attr_course_students_group'],
                element_get_text(row[7]).strip(), roster_path)

            student_file.finish()
            student_filename = '{}.json'.format(student_id)
            self.add(student_filename, student_file)
            collected_accounts[student_id] = None

        for account in collected_accounts.keys():
            self.add(account, InternalLink(self.vfs, self,
                self.vfs.root.students.add_student(
                    account, sn=self._course_sn, pwd=self)))

        self.ready = True

class CourseAssistantsDirectory(Directory):
    def __init__(self, vfs, parent, cell):
        super().__init__(vfs, parent)
        self._cell = cell

    def fetch(self):
        s = self.vfs.strings

        assert not element_get_text(self._cell).strip()
        for child in self._cell:
            assert child.tag == 'a' or child.tag == 'br'

        collected_accounts = OrderedDict()
        for index, link in enumerate(self._cell.xpath('./a')):
            assistant_name = link.text
            assistant_email = link.get('href')
            assert assistant_name
            assert assistant_email.startswith('mailto:')
            assistant_email = assistant_email[7:]

            assistant_file = JSONFile(self.vfs, self)
            assistant_filename = '{:02} {}.json'.format(
                index + 1, assistant_name)

            assistant_file.add(s['attr_course_assistants_name'],
                assistant_name, '/student/index.php')
            assistant_file.add(s['attr_course_assistants_email'],
                assistant_email, '/student/index.php')

            assistant_file.finish()
            self.add(assistant_filename, assistant_file)

            if assistant_email.endswith('@ntu.edu.tw') or \
                assistant_email.endswith('@csie.ntu.edu.tw'):
                collected_accounts[assistant_email.split('@')[0]] = None

        for account in collected_accounts.keys():
            self.add(account, InternalLink(self.vfs, self,
                self.vfs.root.students.add_student(account, pwd=self)))

        self.ready = True

class JSONFile(Regular):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
        self._json = OrderedDictWithLineBreak()
        self._sources = OrderedDictWithLineBreak()

    def add(self, key, value, source):
        assert key not in self._json
        assert key not in self._sources
        assert isinstance(source, str) or isinstance(source, list)
        self._json[key] = value
        self._sources[key] = source

    def append(self, key, value):
        assert key in self._json
        assert isinstance(self._json[key], list)
        self._json[key].append(value)

    def replace(self, key, value, source=None):
        assert key in self._json
        assert key in self._sources
        self._json[key] = value
        if source:
            assert isinstance(source, str) or isinstance(source, list)
            self._sources[key] = source

    def get(self, key):
        return self._json[key]

    def finish(self, indent=2):
        self._content = json.dumps([ self._json, self._sources ],
            ensure_ascii=False, allow_nan=False, indent=indent) + '\n'
        del self._json
        del self._sources
        self.ready = True

class CSVFile(Regular):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
        self._csv = StringIO()
        self._writer = csv.writer(self._csv, dialect='unix')

    def add(self, row):
        self._writer.writerow(row)

    def finish(self):
        self._content = self._csv.getvalue()
        del self._writer
        del self._csv
        self.ready = True

class StringFile(Regular):
    def __init__(self, vfs, parent, content):
        super().__init__(vfs, parent)
        self._content = content
        self.ready = True

class BytesFile(Regular):
    def __init__(self, vfs, parent, bytes_content):
        super().__init__(vfs, parent)
        self._bytes_content = bytes_content
        self.ready = True

    def read(self, output, progress_callback=lambda *x: None):
        progress_callback(False, None, None, None)
        output.write(self._bytes_content)
        progress_callback(True, None, None, None)

    def size(self):
        return len(self._bytes_content)

class DownloadFile(Regular):
    def __init__(self, vfs, parent, path, args={}):
        super().__init__(vfs, parent)
        self._path = path
        self._args = args
        self.local = False
        self.ready = True

    def read(self, output, progress_callback=lambda *x: None):
        self.vfs.request.file(
            self._path, output, args=self._args,
            progress_callback=progress_callback)

    def size(self):
        return self.vfs.request.file_size(self._path, args=self._args)

class StateDownloadFile(Regular):
    def __init__(self, vfs, parent, path, args={}, steps=[]):
        super().__init__(vfs, parent)
        self._path = path
        self._args = args
        self._steps = steps
        self.local = False
        self.ready = True

    def read(self, output, progress_callback=lambda *x: None):
        for step_path, step_args in self._steps:
            self.vfs.request.file(step_path, BytesIO(), args=step_args)
        self.vfs.request.file(
            self._path, output, args=self._args,
            progress_callback=progress_callback)

    def size(self):
        for step_path, step_args in self._steps:
            self.vfs.request.file(step_path, BytesIO(), args=step_args)
        return self.vfs.request.file_size(self._path, args=self._args)
