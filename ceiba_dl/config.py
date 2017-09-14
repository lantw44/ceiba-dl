# License: LGPL3+

from tempfile import NamedTemporaryFile
import ast
import configparser
import logging
import os.path
import shutil
import xdg.BaseDirectory

class Config:

    defaults = {
        'api_cookies': { },
        'web_cookies': { },
        'edit': {
            'add_courses': [ ],
            'add_unenrolled_courses': [ ],
            'delete_files': [ ]
         },
        'strings': {
            'dir_root_courses': '課程',
            'dir_root_students': '學生',
            'dir_root_teachers': '教師',
            'link_semester_current': '目前',
            'file_course_metadata': '課程資訊.json',
            'attr_course_metadata_name': '名稱',
            'attr_course_metadata_class_no': '班次',
            'attr_course_metadata_sn': '序號',
            'attr_course_metadata_time': '時間',
            'attr_course_metadata_lang': '語言',
            'value_course_metadata_lang_big5': '中文',
            'value_course_metadata_lang_eng': '英文',
            'attr_course_metadata_code': '課號',
            'attr_course_metadata_place': '地點',
            'attr_course_metadata_mark': '備註',
            'attr_course_metadata_evaluation': '評量方式',
            'attr_course_metadata_evaluation_item': '項目',
            'attr_course_metadata_evaluation_percent': '百分比',
            'attr_course_metadata_evaluation_notes': '說明',
            'dir_course_boards': '討論看板',
            'file_course_boards_metadata': '看板資訊.json',
            'attr_course_boards_metadata_sn': '序號',
            'attr_course_boards_metadata_caption': '看板名稱',
            'attr_course_boards_thread_sn': '序號',
            'attr_course_boards_thread_parent': '上篇序號',
            'attr_course_boards_thread_subject': '主題',
            'attr_course_boards_thread_post_time': '發表時間',
            'attr_course_boards_thread_attach': '檔案名稱',
            'attr_course_boards_thread_content': '檔案路徑',
            'attr_course_boards_thread_author': '作者帳號',
            'attr_course_boards_thread_cauthor': '作者姓名',
            'attr_course_boards_thread_count_rep': '回覆篇數',
            'attr_course_boards_thread_latest_rep': '最後回覆時間',
            'dir_course_boards_thread_files': '檔案',
            'dir_course_bulletin': '公佈欄',
            'dir_course_bulletin_attachments': '附加檔案',
            'attr_course_bulletin_sn': '序號',
            'attr_course_bulletin_subject': '公告主題',
            'attr_course_bulletin_date': '公告日期',
            'attr_course_bulletin_url': '相關網址',
            'attr_course_bulletin_attachment': '相關附檔',
            'attr_course_bulletin_content': '公告內容',
            'dir_course_contents': '課程內容',
            'dir_course_contents_files': '內容檔案',
            'attr_course_contents_sn': '序號',
            'attr_course_contents_week': '週次',
            'attr_course_contents_date': '日期',
            'attr_course_contents_subject': '單元主題',
            'attr_course_contents_files': '內容檔案',
            'dir_course_grades': '學習成績',
            'file_course_grades_table': '成績列表.csv',
            'attr_course_grades_main_sn': '主項序號',
            'attr_course_grades_sub_sn': '子項序號',
            'attr_course_grades_tier': '類型',
            'value_course_grades_tier_0': '出缺席紀錄',
            'value_course_grades_tier_1': '主項目（不含子項目）',
            'value_course_grades_tier_2': '主項目（包含子項目）',
            'value_course_grades_tier_3': '學期成績',
            'value_course_grades_tier_': '子項目',
            'attr_course_grades_item': '項目',
            'attr_course_grades_percent': '比重',
            'attr_course_grades_grade_isranking': '評分方式',
            'value_course_grades_grade_isranking_0': '百分制',
            'value_course_grades_grade_isranking_1': '等第制',
            'value_course_grades_grade_isranking_': '未知',
            'attr_course_grades_notes': '說明',
            'attr_course_grades_grade': '得分',
            'attr_course_grades_evaluation': '評語',
            'attr_course_grades_show': '成績公布',
            'value_course_grades_show_n': '不公布',
            'value_course_grades_show_p': '公布個人',
            'attr_course_grades_is_changed': 'is_changed',
            'dir_course_homeworks': '作業區',
            'file_course_homeworks_homework': '作業內容.json',
            'attr_course_homeworks_homework_sn': '序號',
            'attr_course_homeworks_homework_name': '名稱',
            'attr_course_homeworks_homework_description': '作業說明',
            'attr_course_homeworks_homework_download_file_path': '相關檔案',
            'attr_course_homeworks_homework_url': '相關網址',
            'attr_course_homeworks_homework_type': '成員',
            'value_course_homeworks_homework_type_individual': '個人',
            'value_course_homeworks_homework_type_group': '小組',
            'attr_course_homeworks_homework_method': '繳交方法',
            'value_course_homeworks_homework_method_online': '線上繳交',
            'value_course_homeworks_homework_method_printed': '紙本繳交',
            'attr_course_homeworks_homework_percentage': '成績比重',
            'attr_course_homeworks_homework_pub': '發布日期',
            'attr_course_homeworks_homework_end': '繳交期限',
            'value_course_homeworks_homework_end_2030_12_31_24': '無限期',
            'attr_course_homeworks_homework_is_subm': '逾期繳交',
            'attr_course_homeworks_homework_is_subm_yes': '可以',
            'attr_course_homeworks_homework_is_subm_no': '不可以',
            'dir_course_homeworks_homework_scores': '作業成績',
            'attr_course_homeworks_homework_hand_time': '繳交日期',
            'attr_course_homeworks_homework_submitted_file_path': '已上傳檔案',
            'attr_course_homeworks_homework_sw': 'sw',
            'attr_course_homeworks_homework_ranking_grade': '等第成績',
            'attr_course_homeworks_homework_score': '百分成績',
            'attr_course_homeworks_homework_evaluation': '作業評語',
            'dir_course_homeworks_homework_eval': '作業評語',
            'file_course_homeworks_homework_eval_table': '作業評語.csv',
            'attr_course_homeworks_homework_eval_id': '學號',
            'attr_course_homeworks_homework_eval_grades': '成績',
            'attr_course_homeworks_homework_eval_comments': '作業評語',
            'dir_course_homeworks_homework_download_files': '相關檔案',
            'dir_course_homeworks_homework_submitted_files': '已上傳檔案',
            'dir_course_homeworks_homework_great_assignments': '作業觀摩',
            'file_course_homeworks_homework_great_assignments_info': '學生資訊.json',
            'attr_course_homeworks_homework_great_assignments_id': '學號',
            'attr_course_homeworks_homework_great_assignments_name': '姓名',
            'dir_course_homeworks_homework_great_assignments_assignment': '作業區',
            'dir_course_share': '資源分享',
            'dir_course_share_url': '網站分享',
            'dir_course_share_book': '書籍分享',
            'dir_course_share_perd': '文章分享',
            'attr_course_share_sn': '序號',
            'attr_course_share_name': '名稱',
            'attr_course_share_author': '分享者',
            'attr_course_share_author_email': '分享者電子郵件',
            'attr_course_share_rating': '評分',
            'attr_course_share_rating_detail': '評分詳細資訊',
            'attr_course_share_views': '點閱數',
            'attr_course_share_url_url': '網址',
            'attr_course_share_url_description': '網站介紹',
            'attr_course_share_book_language': '語言',
            'attr_course_share_book_edition': '版本',
            'attr_course_share_book_author': '作者',
            'attr_course_share_book_publisher': '出版社',
            'attr_course_share_book_published_date_year': '出版年',
            'attr_course_share_book_published_date_month': '出版月',
            'attr_course_share_book_books_introduction': '書籍介紹',
            'attr_course_share_perd_periodical_title': '期刊名稱',
            'attr_course_share_perd_author': '作者',
            'attr_course_share_perd_publisher': '出版社',
            'attr_course_share_perd_published_date_year': '出版年',
            'attr_course_share_perd_published_date_month': '出版月',
            'attr_course_share_perd_published_date_date': '出版日',
            'attr_course_share_perd_frequency': '發刊週期',
            'value_course_share_perd_frequency_1': '週刊',
            'value_course_share_perd_frequency_2': '雙週刊',
            'value_course_share_perd_frequency_3': '月刊',
            'value_course_share_perd_frequency_4': '季刊',
            'value_course_share_perd_frequency_5': '年刊',
            'value_course_share_perd_frequency_6': '其他',
            'attr_course_share_perd_articles_introduction': '文章介紹',
            'dir_course_vote': '投票區',
            'attr_course_vote_ann_date': '公告日期',
            'attr_course_vote_topic': '投票主題',
            'attr_course_vote_start_date': '開始日期',
            'attr_course_vote_end_date': '結束日期',
            'attr_course_vote_teachers_total': '教師總數',
            'attr_course_vote_teachers_voted': '已投票教師數',
            'attr_course_vote_teachers_not_yet': '未投票教師數',
            'attr_course_vote_tas_total': '助教總數',
            'attr_course_vote_tas_voted': '已投票助教數',
            'attr_course_vote_tas_not_yet': '未投票助教數',
            'attr_course_vote_students_total': '學生總數',
            'attr_course_vote_students_voted': '已投票學生數',
            'attr_course_vote_students_not_yet': '未投票學生數',
            'attr_course_vote_votes_for_each': '每人投票數',
            'attr_course_vote_votes_in_total': '總計投票數',
            'attr_course_vote_result': '分佈',
            'attr_course_vote_result_option': '選項',
            'attr_course_vote_result_votes': '票數',
            'attr_course_vote_result_percent': '比例',
            'dir_course_students': '修課學生',
            'attr_course_students_role': '身份',
            'attr_course_students_department': '系所',
            'attr_course_students_id': '學號',
            'attr_course_students_name': '姓名',
            'attr_course_students_english_name': '英文姓名',
            'attr_course_students_email': '電子郵件',
            'attr_course_students_group': '組別',
            'dir_course_teachers': '教師資訊',
            'attr_course_teachers_account': '帳號',
            'attr_course_teachers_tr_msid': '教師類別',
            'value_course_teachers_tr_msid_0': '主要授課教師',
            'value_course_teachers_tr_msid_1': '其他授課教師',
            'attr_course_teachers_cname': '中文姓名',
            'attr_course_teachers_ename': '英文姓名',
            'attr_course_teachers_email': '電子郵件',
            'attr_course_teachers_phone': '聯絡電話',
            'attr_course_teachers_address': '辦公室',
            'dir_course_teaching_assistants': '課程助教',
            'dir_course_web_assistants': '網頁助教',
            'attr_course_assistants_name': '姓名',
            'attr_course_assistants_email': '電子郵件',
            'attr_students_role': '身份',
            'attr_students_photo': '照片',
            'attr_students_name': '姓名',
            'attr_students_english_name': '英文姓名',
            'attr_students_screen_name': '匿名代號',
            'attr_students_school_year': '學校系級',
            'attr_students_homepage_url': '個人首頁網址',
            'attr_students_email_address': '電子郵件',
            'attr_students_frequently_used_email': '常用電子郵件',
            'attr_students_phone': '聯絡電話',
            'attr_students_address': '聯絡地址',
            'attr_students_more_personal_information': '更多的個人資訊',
            'attr_teachers_name': '姓名',
            'attr_teachers_department': '所屬院系所',
            'attr_teachers_title': '職稱',
            'attr_teachers_url': '個人首頁網址',
            'attr_teachers_email': '電子郵件',
            'attr_teachers_phone': '聯絡電話',
            'attr_teachers_phone_others': '其他電話',
            'attr_teachers_office': '辦公室',
            'attr_teachers_picture': '照片',
            'attr_teachers_more': '更多的個人資訊'
        }
    }

    def __init__(self, name='ceiba-dl', profile='default'):
        self._config = configparser.ConfigParser(interpolation=None)
        self._config.update(Config.defaults)
        self._config.optionxform = str
        self._logger = logging.getLogger(__name__)
        self.name = name
        self.profile = profile

    def load(self):
        # 我們只有個人設定檔，沒有全域設定檔
        conf_path = xdg.BaseDirectory.load_first_config(self.name, self.profile)

        # 檔案不存在就算了
        if not conf_path:
            return True

        self._logger.info('準備讀取設定檔 {}'.format(conf_path))

        try:
            conf_file = open(conf_path, 'r')
        except IOError as err:
            self._logger.error('無法開啟設定檔：{}'.format(err))
            return True

        try:
            self._config.read_file(conf_file)
        except configparser.Error as err:
            self._logger.error('無法載入設定檔：{}'.format(err))
            conf_file.close()
            return False

        conf_file.close()
        self._logger.info('設定值已載入自 {}'.format(conf_path))
        return self.validate()

    def store(self):
        # 確保上層資料夾存在
        conf_dir = xdg.BaseDirectory.save_config_path(self.name)
        conf_path = os.path.join(conf_dir, self.profile)

        # 寫入前先備份檔案
        def open_without_io_error(*args):
            try:
                return open(*args)
            except IOError:
                return None

        conf_file = open_without_io_error(conf_path, 'r')
        if conf_file:
            backup_ok = True
            try:
                with NamedTemporaryFile(mode='w', dir=conf_dir, delete=False) as backup_file:
                    backup_path = backup_file.name
                    shutil.copyfileobj(conf_file, backup_file)
            except IOError as err:
                self._logger.error('無法備份設定檔至 {}：{}'.format(
                    err.filename, err.strerror))
                backup_ok = False
            finally:
                conf_file.close()
            if not backup_ok:
                return False
        del conf_file

        self._logger.info('準備寫入設定檔 {}'.format(conf_path))

        store_ok = True
        try:
            verb = '開啟'
            conf_file = open(conf_path, 'w')
            verb = '寫入'
            self._config.write(conf_file)
            verb = '關閉'
            conf_file.close()
        except IOError as err:
            self._logger.error('無法{}設定檔：{}'.format(verb, err))
            store_ok = False
        finally:
            conf_file.close()
        if not store_ok:
            return False

        self._logger.info('設定值已儲存至 {}'.format(conf_path))

        try:
            os.unlink(backup_path)
        except (NameError, IOError):
            pass
        return True

    def validate(self):
        for section in self._config.sections():
            if section not in Config.defaults.keys():
                self._logger.warning('設定檔中有不明的區段 {}'.format(section))
                continue
            if len(Config.defaults[section]) == 0:
                continue
            for key in self._config[section].keys():
                if key not in Config.defaults[section].keys():
                    self._logger.warning('設定檔 {} 區段中有不明的名稱 {}'.format(
                        section, key))
        return True

    @property
    def api_cookies(self):
        return dict(self._config['api_cookies'])

    @api_cookies.setter
    def api_cookies(self, value):
        self._config['api_cookies'] = {}
        self._config['api_cookies'].update(value)

    @property
    def web_cookies(self):
        return dict(self._config['web_cookies'])

    @web_cookies.setter
    def web_cookies(self, value):
        self._config['web_cookies'] = {}
        self._config['web_cookies'].update(value)

    @property
    def edit(self):
        edit = dict(self._config['edit'])
        for key in ['add_courses', 'add_unenrolled_courses', 'delete_files']:
            edit[key] = ast.literal_eval(edit[key])
        return edit

    @property
    def strings(self):
        return dict(self._config['strings'])
