#!@PYTHON@
#
# NTU CEIBA data downloading tool
# Copyright (C) 2017  Ting-Wei Lan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import argparse
import logging
import sys

pythondir = '@pythondir@'
if not pythondir.startswith('@'):
    if pythondir not in sys.path:
        sys.path.append(pythondir)

from ceiba_dl.config import Config

def progress_callback(path, total_to_download, downloaded, *args):
    if downloaded == None:
        if not total_to_download:
            sys.stderr.write('\r{}: 0%'.format(path))
        else:
            sys.stderr.write('\r{}: 100%'.format(path))
    else:
        total_to_download_mib = total_to_download / 2**20
        downloaded_mib = downloaded / 2**20
        if downloaded != total_to_download:
            sys.stderr.write('\r{}: {}% ({:.2f}/{:.2f} MiB)'.format(path,
                downloaded * 100 // total_to_download,
                downloaded_mib, total_to_download_mib))
        elif downloaded == 0:
            sys.stderr.write('\r{}: 0% ({:.2f}/{:.2f} MiB)'.format(path,
                downloaded_mib, total_to_download_mib))
        else:
            sys.stderr.write('\r{}: 100% ({:.2f}/{:.2f} MiB)'.format(path,
                downloaded_mib, total_to_download_mib))

def run_api(args, config):
    from ceiba_dl import Request, Error
    logger = logging.getLogger('ceiba-dl-api')

    request = Request(config.api_cookies, config.web_cookies)
    query_fields = dict()
    for field in args.field:
        query_fields[field[0]] = field[1]

    try:
        result = request.api(query_fields)
    except Error as err:
        logger.error(err)
        return False

    from pprint import pprint
    pprint(result)
    return True

def run_cat(args, config):
    from ceiba_dl import Request, Cat, Error
    from ceiba_dl.vfs import VFS
    from time import monotonic
    logger = logging.getLogger('ceiba-dl-cat')

    if len(args.file) == 0:
        return True

    request = Request(config.api_cookies, config.web_cookies)
    vfs = VFS(request, config.strings, config.edit)
    cat = Cat(vfs)
    failed = False
    for path in args.file:
        try:
            progress_callback_used = False
            last_update_time = 0
            def cat_progress_callback(total_to_download, downloaded, *args):
                nonlocal progress_callback_used
                nonlocal last_update_time
                if downloaded != None:
                    current_time = monotonic()
                    if total_to_download == downloaded or \
                       current_time - last_update_time >= 0.1:
                        progress_callback_used = True
                        last_update_time = current_time
                        progress_callback(
                            path, total_to_download, downloaded, *args)
            cat.run(sys.stdout.buffer, path,
                progress_callback=cat_progress_callback)
            if progress_callback_used:
                sys.stderr.write('\n')
        except Error as err:
            failed = True
            logger.error(err)
    return not failed

def run_get(args, config):
    from ceiba_dl import Request, Get
    from ceiba_dl.vfs import VFS
    from time import monotonic
    logger = logging.getLogger('ceiba-dl-get')

    if len(args.file) == 0:
        args.file.append('/')

    request = Request(config.api_cookies, config.web_cookies)
    vfs = VFS(request, config.strings, config.edit)
    get = Get(vfs, logger)
    succeeded = True
    for path in args.file:
        last_progress_update = 0

        def download_callback(path, total_to_download, downloaded, *args):
            nonlocal last_progress_update
            current = monotonic()
            if downloaded == None or total_to_download == downloaded or \
                current - last_progress_update >= 0.1:
                last_progress_update = current
                progress_callback(path, total_to_download, downloaded, *args)

        def end_callback(path):
            nonlocal last_progress_update
            last_progress_update = 0
            sys.stderr.write('\n')

        if args.no_progress:
            succeeded = succeeded and get.run(path, retry=args.retry)
        else:
            succeeded = succeeded and get.run(path, retry=args.retry,
                download_progress_callback=download_callback,
                end_download_callback=end_callback)
    return succeeded

def run_ls(args, config):
    from ceiba_dl import Request, Ls, Error
    from ceiba_dl.vfs import VFS
    logger = logging.getLogger('ceiba-dl-ls')

    if len(args.file) == 0:
        args.file.append('/')

    request = Request(config.api_cookies, config.web_cookies)
    vfs = VFS(request, config.strings, config.edit)
    lser = Ls(vfs, details=args.long, recursive=args.recursive)
    failed = False
    for path in args.file:
        try:
            lser.run(sys.stdout, path)
        except Error as err:
            failed = True
            logger.error(err)
    return not failed

def run_login(args, config):
    from ceiba_dl.helper import Login
    login = Login(config, main_script=__file__, store=not args.dry_run)
    return login.run()

if __name__ == '__main__':
    try:
        import ceiba_dl._version
        version = ' 版本 {}'.format(ceiba_dl._version.version)
    except ImportError:
        version = ''
    app = argparse.ArgumentParser(add_help=False,
        description='NTU CEIBA 資料下載工具' + version)
    sub = app.add_subparsers(title='可用的子指令')
    cmd_api = sub.add_parser('api', help='直接使用 NTU CEIBA API')
    cmd_api.set_defaults(func=run_api)
    cmd_api.add_argument('field', nargs='*',
        type=lambda x: tuple(x.split('=', 1)) if x.find('=') >= 0 else (x, ''),
        help='要使用 query string 傳入的參數，格式是 name=value')
    cmd_cat = sub.add_parser('cat', help='顯示資料')
    cmd_cat.set_defaults(func=run_cat)
    cmd_cat.add_argument('file', nargs='*', type=str,
        help='要查看的檔案名稱')
    cmd_get = sub.add_parser('get', help='下載資料')
    cmd_get.set_defaults(func=run_get)
    cmd_get.add_argument('-s', '--no-progress', action='store_true',
        help='不要顯示下載進度列')
    cmd_get.add_argument('-t', '--retry',
        type=lambda x: int(x) if int(x) >= 0 else 0, default=3,
        help='自動重試的次數')
    cmd_get.add_argument('file', nargs='*', type=str,
        help='要下載的檔案名稱')
    cmd_ls = sub.add_parser('ls', help='列出可供下載的資料')
    cmd_ls.set_defaults(func=run_ls)
    cmd_ls.add_argument('-l', '--long', action='store_true',
        help='顯示檔案詳細資訊')
    cmd_ls.add_argument('-r', '--recursive', action='store_true',
        help='遞迴列出子目錄')
    cmd_ls.add_argument('file', nargs='*', type=str,
        help='要查看的資料夾名稱')
    cmd_login = sub.add_parser('login', help='登入網站')
    cmd_login.set_defaults(func=run_login)
    cmd_login.add_argument('-n', '--dry-run', action='store_true',
        help='測試模式：不要將取得的登入資訊寫入設定檔')
    opt = app.add_argument_group(title='可用的選項')
    opt.add_argument('--help', action='help',
        help='顯示說明訊息並離開')
    opt.add_argument('--log-level', action='store', metavar='層級',
        choices=['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'],
        help='要記錄的訊息層級', default='WARNING')
    opt.add_argument('--log-time', action='store_true',
        help='記錄訊息產生的時間')
    opt.add_argument('-p', '--profile', action='store', metavar='設定檔',
        help='選擇要使用的設定檔', default='default')
    opt.add_argument('-v', '--verbose', action='store_true',
        help='顯示各項操作詳細資訊')
    args = app.parse_args(sys.argv[1:])

    # 把使用者輸入的字串轉成 logging 想要的數字
    log_level_number = getattr(logging, args.log_level)
    # -v 跟 --log-level=INFO 是一樣的意思
    if args.verbose and log_level_number > logging.INFO:
        log_level_number = logging.INFO
    if args.log_time:
        log_format = '%(asctime)s - %(name)s: <%(levelname)s> %(message)s'
    else:
        log_format = '%(name)s: <%(levelname)s> %(message)s'
    # 這是個互動式的程式，所以訊息直接印到終端機上就好了
    logging.basicConfig(level=log_level_number,
        format=log_format, datefmt='%Y-%m-%d %H:%M:%S')

    if not hasattr(args, 'func'):
        logging.error('沒有指定子指令')
        exit(1)

    config = Config(profile=args.profile)
    if not config.load():
        exit(1)

    exit(0 if args.func(args, config) else 1)
