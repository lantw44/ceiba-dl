# License: LGPL3+

import logging
import os
import subprocess
import xdg.BaseDirectory

class Helper:

    default_keys = [
        'PHPSESSID',
        'user'
    ]

    def __init__(self, name):
        self._name = name
        self._keys = Helper.default_keys
        self._cookies = dict()

    def __str__(self):
        return self._name

    @property
    def name(self):
        return self._name

    @property
    def cookies(self):
        return self._cookies

class BuiltinHelper(Helper):
    def __init__(self):
        super().__init__('')

    def __str__(self):
        return '<Builtin>'

    def run(self, usage):
        if usage == 'API':
            url = 'https://ceiba.ntu.edu.tw/course/f03067/app/info_web.php?api_version=2'
        elif usage == 'Web':
            url = 'https://ceiba.ntu.edu.tw/ChkSessLib.php'
        else:
            assert False
        print('請使用網址 {} 登入 NTU CEIBA 後輸入 cookie 的值'.format(url))
        for cn in self._keys:
            try:
                self._cookies[cn] = input('{} {}: '.format(usage, cn))
            except EOFError:
                return False
        return True

class ExternalHelper(Helper):
    def __init__(self, path):
        super().__init__(os.path.basename(path))
        self.cmd = path

    def __str__(self):
        return '<External> ' + self.cmd

    def run(self, usage):
        args = [ self.cmd, 'NTU CEIBA 登入輔助程式', usage ]
        try:
            helper = subprocess.Popen(args, bufsize=1, universal_newlines=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except IOError:
            return False

        try:
            if usage == 'API':
                helper.stdin.write('https://ceiba.ntu.edu.tw/course/f03067/app/info_web.php?api_version=2\n')
                helper.stdin.write('https://ceiba.ntu.edu.tw/course/f03067/app/login.php\n')
            elif usage == 'Web':
                helper.stdin.write('https://ceiba.ntu.edu.tw/ChkSessLib.php\n')
                helper.stdin.write('https://ceiba.ntu.edu.tw\n')
            else:
                assert False
        except BrokenPipeError:
            return False

        okay = helper.stdout.readline().strip()
        if okay != 'OK':
            return False

        for cn in self._keys:
            try:
                helper.stdin.write(cn + '\n')
            except BrokenPipeError:
                return False
            cookie_value_with_newline = helper.stdout.readline()
            if cookie_value_with_newline == '':
                return False
            self._cookies[cn] = cookie_value_with_newline.strip()
        helper.stdin.write('\n')

        if helper.wait() != 0:
            return False

        return True

class Login:
    def __init__(self, config, store=True, main_script=None, helpers_dir='helpers'):
        self.config = config
        self.store = store
        self.logger = logging.getLogger(__name__)

        helper_path = list(xdg.BaseDirectory.load_data_paths(
            self.config.name, helpers_dir))

        try:
            import ceiba_dl._version
            personal_helper_path = os.path.join(xdg.BaseDirectory.xdg_data_home,
                self.config.name, helpers_dir)
            if os.path.isdir(ceiba_dl._version.helperdir):
                if personal_helper_path in helper_path:
                    assert helper_path[0] == personal_helper_path
                    helper_path.insert(1, ceiba_dl._version.helperdir)
                else:
                    helper_path.insert(0, ceiba_dl._version.helperdir)
        except ImportError:
            pass

        if main_script:
            main_script_dir = os.path.dirname(os.path.abspath(main_script))
            uninstalled_helpers_dir = os.path.join(main_script_dir, helpers_dir)
            if os.path.isdir(uninstalled_helpers_dir):
                helper_path.append(uninstalled_helpers_dir)
        self.logger.info('輔助程式搜尋路徑是 {}'.format(helper_path))

        self.helpers = []
        for helper_dir in helper_path:
            for helper in sorted(os.listdir(helper_dir)):
                self.helpers.append(
                    ExternalHelper(os.path.join(helper_dir, helper)))
        self.helpers.append(BuiltinHelper())

        for helper in self.helpers:
            self.logger.info('可用的輔助程式：{}'.format(helper))

    def run(self):
        used = {}
        for helper in self.helpers:
            if helper.name in used:
                self.logger.info('忽略同名輔助程式 {}'.format(helper))
                continue
            else:
                self.logger.info('嘗試執行輔助程式 {}'.format(helper))
            api_result = helper.run('API')
            api_cookies = dict(helper.cookies)
            web_result = helper.run('Web')
            web_cookies = dict(helper.cookies)
            if api_result and web_result:
                if self.store:
                    self.config.api_cookies = api_cookies
                    self.config.web_cookies = web_cookies
                    self.config.store()
                return True
            used[helper.name] = True
        self.logger.error('無法透過輔助程式取得 CEIBA 登入資訊')
        return False
