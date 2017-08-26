# License: LGPL3+

from collections import OrderedDict
from io import BytesIO, StringIO
from lxml import etree
from pathlib import PurePosixPath
from urllib.parse import urlencode, urlsplit, parse_qs
import csv
import json
import logging

# 提供給外部使用的 VFS 界面

class VFS:
    def __init__(self, request, strings, edit):
        self.logger = logging.getLogger(__name__)
        self.request = request
        self.strings = strings
        self.root = RootDirectory(self)
        self._edit = edit

    def open(self, path, cwd=None, edit_check=True):
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
            node = self.open(path, edit_check=False)
            if node is self.root:
                raise ValueError('不可以刪除根資料夾')
            node.parent.unlink(PurePosixPath(path).name)

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

def url_to_path_and_args(url):
    components = urlsplit(url)
    path = components.path
    query_string = components.query
    args = parse_qs(query_string, keep_blank_values=True)
    for key, value in args.items():
        if isinstance(value, list):
            assert len(value) == 1
            args[key] = value[0]
    return (path, args)

# lxml 遇到沒有文字時回傳 None，但空字串比較好操作

def element_get_text(element):
    return '' if element.text == None else element.text

# 從雙欄表格中取得資料的輔助工具

def row_get_value(row, expected_keys, value_mappings,
    free_form=False, return_object=False):

    assert len(row) == 2
    assert row[0].tag == 'th'
    assert row[1].tag == 'td'
    assert row[0].text in expected_keys
    for source, mapped in value_mappings.items():
        if row[1].text in source:
            return mapped
    if free_form:
        if return_object:
            return row[1]
        else:
            return element_get_text(row[1])
    else:
        assert False

# 在真正開始爬網頁前先檢查功能是否開啟

def ceiba_function_enabled(request, course_sn, function, path):
    frame_path = '/modules/index.php'
    frame_args = {'csn': course_sn, 'default_fun': function}
    request.web(frame_path, args=frame_args)
    return len(request.web(path).xpath('//table')) > 0

# 基本的檔案型別：普通檔案、目錄、內部連結、外部連結

class File:
    def __init__(self, vfs, parent):
        self.children = None
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
        self.children = list()

    def read(self, output, **kwargs):
        if not self.ready:
            self.fetch()
        output.write(str(self.children).encode() + b'\n')

    def list(self):
        if not self.ready:
            self.fetch()
        return list(self.children)

    def add(self, name, node, ignore_duplicate=False):
        assert node.parent is self
        assert node.vfs is self.vfs
        name = name.replace('/', '_').strip()
        if name in map(lambda x: x[0], self.children):
            if ignore_duplicate:
                return
            else:
                raise FileExistsError('檔案 {} 已經存在了'.format(name))
        self.children.append((name, node))

    def access(self, name):
        if name == '.':
            return self
        elif name == '..':
            return self.parent
        else:
            if not self.ready:
                self.fetch()
            for child in self.children:
                if child[0] == name:
                    return child[1]
            raise FileNotFoundError('在目前的目錄下找不到 {} 檔案'.format(name))

    def unlink(self, name):
        for index, child in enumerate(self.children):
            if child[0] == name:
                del self.children[index]
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
        self.add(s['dir_root_courses'], RootCoursesDirectory(self.vfs, self))
        self.add(s['dir_root_students'], RootStudentsDirectory(self.vfs, self))
        self.add(s['dir_root_teachers'], RootTeachersDirectory(self.vfs, self))
        self.ready = True

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

class RootStudentsDirectory(Directory):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
        self.ready = True

class RootTeachersDirectory(Directory):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
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
                .format(self._sn, self._name, info_basic_name, self._semester))

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
        from html import escape
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
                    '    <title>{}</title>'.format(escape(post['subject'])),
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
        import ast
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
                student_hw_script_ast = ast.parse(student_hw_script)
                assert len(student_hw_script_ast.body) == 1
                assert type(student_hw_script_ast.body[0]) == ast.Expr
                assert type(student_hw_script_ast.body[0].value) == ast.Call
                assert student_hw_script_ast.body[0].value.func.value.id == 'window'
                assert student_hw_script_ast.body[0].value.func.attr == 'open'
                assert len(student_hw_script_ast.body[0].value.args) == 3
                student_hw_link = student_hw_script_ast.body[0].value.args[0].s

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

class JSONFile(Regular):
    def __init__(self, vfs, parent):
        super().__init__(vfs, parent)
        self._json = OrderedDictWithLineBreak()
        self._sources = OrderedDictWithLineBreak()

    def add(self, key, value, source):
        assert key not in self._json
        assert key not in self._sources
        self._json[key] = value
        self._sources[key] = source

    def append(self, key, value):
        assert key in self._json
        assert isinstance(self._json[key], list)
        self._json[key].append(value)

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
