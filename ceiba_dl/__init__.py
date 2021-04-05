# License: LGPL3+

from lxml import etree
import errno
import io
import json
import logging
import os
import pathlib
import pycurl
import urllib.parse

from pathvalidate import sanitize_filepath

class Error(Exception):
    def __str__(self):
        return self.message

class ServerError(Error):
    def __init__(self, status):
        from http import HTTPStatus
        self.status = status
        try:
            phrase = HTTPStatus(status).phrase
            self.message = '伺服器回傳 HTTP 狀態 {} ({})'.format(status, phrase)
        except ValueError:
            self.message = '伺服器回傳 HTTP 狀態 {}'.format(status)

class NotJSONError(Error):
    def __init__(self, data):
        self.response = data
        self.message = '伺服器回傳非 JSON 格式資料：{}'.format(
            data.strip().replace('\r', '').replace('\n', ' '))

class NoneIO:
    def write(*x):
        pass

class Request:
    def __init__(self, api_cookies, web_cookies, cipher=None, api_args={'api': '1'},
        api_url='https://ceiba.ntu.edu.tw/course/f03067/app/login.php',
        file_url='https://ceiba.ntu.edu.tw',
        web_url='https://ceiba.ntu.edu.tw'):

        self.logger = logging.getLogger(__name__)
        self.curl = pycurl.Curl()
        self.api_cookie = ';'.join(map(lambda x: '{}={}'.format(*x), api_cookies.items()))
        self.web_cookie = ';'.join(map(lambda x: '{}={}'.format(*x), web_cookies.items()))
        self.api_args = api_args
        self.api_url = api_url
        self.file_url = file_url
        self.web_url = web_url
        self.api_cache = None
        self.web_cache = dict()
        if not cipher:
            tls_backend = pycurl.version_info()[5].split('/')[0]
            if tls_backend == 'OpenSSL' or tls_backend == 'LibreSSL':
                cipher = 'ECDHE-RSA-AES128-GCM-SHA256'
            elif tls_backend == 'GnuTLS':
                cipher = 'ECDHE-RSA-AES128-GCM-SHA256'
            elif tls_backend == 'NSS':
                cipher = 'ecdhe_rsa_aes_128_gcm_sha_256'
            else:
                assert False, 'TLS 實作 {} 尚未支援'.format(tls_backend)
        self.curl.setopt(pycurl.USE_SSL, pycurl.USESSL_ALL)
        self.curl.setopt(pycurl.SSL_CIPHER_LIST, cipher)
        self.curl.setopt(pycurl.PROTOCOLS, pycurl.PROTO_HTTPS)
        self.curl.setopt(pycurl.REDIR_PROTOCOLS, pycurl.PROTO_HTTPS)
        self.curl.setopt(pycurl.DEFAULT_PROTOCOL, 'https')
        self.curl.setopt(pycurl.FOLLOWLOCATION, False)

    def api(self, args, encoding='utf-8', allow_return_none=False):
        self.logger.debug('準備送出 API 請求')
        if args.get('mode', '') == 'semester':
            semester = args.get('semester', '')
            if allow_return_none and self.api_cache == semester:
                self.logger.debug('忽略重複的 {} 學期 API 請求'.format(semester))
                return
            self.api_cache = semester
        query_args = dict()
        query_args.update(self.api_args)
        query_args.update(args)
        url = self.api_url + '?' + urllib.parse.urlencode(query_args)
        data = io.BytesIO()
        self.logger.debug('HTTP 請求網址：{}'.format(url))
        self.curl.setopt(pycurl.URL, url)
        self.curl.setopt(pycurl.COOKIE, self.api_cookie)
        self.curl.setopt(pycurl.NOBODY, False)
        self.curl.setopt(pycurl.NOPROGRESS, True)
        self.curl.setopt(pycurl.WRITEDATA, data)
        self.curl.setopt(pycurl.HEADERFUNCTION, lambda *x: None)
        self.curl.setopt(pycurl.XFERINFOFUNCTION, lambda *x: None)
        self.curl.perform()
        status = self.curl.getinfo(pycurl.RESPONSE_CODE)
        if status != 200:
            raise ServerError(status)
        try:
            value = data.getvalue()
            return json.loads(value.decode(encoding))
        except json.decoder.JSONDecodeError:
            raise NotJSONError(value.decode(encoding))

    def file(self, path, output, args={}, progress_callback=lambda *x: None):
        self.logger.debug('準備送出檔案下載請求')
        self.web_cache[path] = dict(args)
        url = urllib.parse.urljoin(self.file_url, urllib.parse.quote(path))
        if len(args) > 0:
            url += '?' + urllib.parse.urlencode(args)
        self.logger.debug('HTTP 請求網址：{}'.format(url))
        self.curl.setopt(pycurl.URL, url)
        self.curl.setopt(pycurl.COOKIE, self.web_cookie)
        self.curl.setopt(pycurl.NOBODY, False)
        self.curl.setopt(pycurl.NOPROGRESS, False)
        self.curl.setopt(pycurl.WRITEDATA, output)
        self.curl.setopt(pycurl.HEADERFUNCTION, lambda *x: None)
        self.curl.setopt(pycurl.XFERINFOFUNCTION, progress_callback)
        self.curl.perform()
        status = self.curl.getinfo(pycurl.RESPONSE_CODE)
        if status != 200:
            raise ServerError(status)

    def file_size(self, path, args={}):
        self.logger.debug('準備送出檔案大小查詢請求')
        self.web_cache[path] = dict(args)
        url = urllib.parse.urljoin(self.file_url, urllib.parse.quote(path))
        if len(args) > 0:
            url += '?' + urllib.parse.urlencode(args)
        self.logger.debug('HTTP 請求網址：{}'.format(url))
        self.curl.setopt(pycurl.URL, url)
        self.curl.setopt(pycurl.COOKIE, self.web_cookie)
        self.curl.setopt(pycurl.NOBODY, True)
        self.curl.setopt(pycurl.NOPROGRESS, True)
        self.curl.setopt(pycurl.WRITEDATA, io.BytesIO())
        self.curl.setopt(pycurl.HEADERFUNCTION, lambda *x: None)
        self.curl.setopt(pycurl.XFERINFOFUNCTION, lambda *x: None)
        self.curl.perform()
        status = self.curl.getinfo(pycurl.RESPONSE_CODE)
        if status != 200:
            raise ServerError(status)
        return self.curl.getinfo(pycurl.CONTENT_LENGTH_DOWNLOAD)

    def web(self, path, args={}, encoding=None, allow_return_none=False):
        self.logger.debug('準備送出網頁請求')
        if allow_return_none:
            if path in self.web_cache and self.web_cache[path] == args:
                self.logger.debug('忽略重複的 {} 網頁請求'.format(path))
                self.logger.debug('參數：{}'.format(args))
                return
        self.web_cache[path] = dict(args)
        url = urllib.parse.urljoin(self.web_url, urllib.parse.quote(path))
        if len(args) > 0:
            url += '?' + urllib.parse.urlencode(args)
        self.logger.debug('HTTP 請求網址：{}'.format(url))
        data = io.BytesIO()
        self.curl.setopt(pycurl.URL, url)
        self.curl.setopt(pycurl.COOKIE, self.web_cookie)
        self.curl.setopt(pycurl.NOBODY, False)
        self.curl.setopt(pycurl.NOPROGRESS, True)
        self.curl.setopt(pycurl.WRITEDATA, data)
        self.curl.setopt(pycurl.HEADERFUNCTION, lambda *x: None)
        self.curl.setopt(pycurl.XFERINFOFUNCTION, lambda *x: None)
        self.curl.perform()
        status = self.curl.getinfo(pycurl.RESPONSE_CODE)
        if status != 200:
            raise ServerError(status)
        data.seek(io.SEEK_SET)
        return etree.parse(data, etree.HTMLParser(
            encoding=encoding, remove_comments=True))

    def web_redirect(self, path, args={}):
        self.logger.debug('準備測試網頁重導向目的地')
        self.web_cache[path] = dict(args)
        url = urllib.parse.urljoin(self.web_url, urllib.parse.quote(path))
        if len(args) > 0:
            url += '?' + urllib.parse.urlencode(args)
        self.logger.debug('HTTP 請求網址：{}'.format(url))
        headers = io.BytesIO()
        self.curl.setopt(pycurl.URL, url)
        self.curl.setopt(pycurl.COOKIE, self.web_cookie)
        self.curl.setopt(pycurl.NOBODY, False)
        self.curl.setopt(pycurl.NOPROGRESS, True)
        self.curl.setopt(pycurl.WRITEDATA, NoneIO())
        self.curl.setopt(pycurl.HEADERFUNCTION, headers.write)
        self.curl.setopt(pycurl.XFERINFOFUNCTION, lambda *x: None)
        self.curl.perform()
        status = self.curl.getinfo(pycurl.RESPONSE_CODE)
        if status != 302:
            raise ServerError(status)
        for header_line in headers.getvalue().split(b'\r\n'):
            if header_line.startswith(b'Location:'):
                return header_line.split(b':', maxsplit=1)[1].strip().decode()
        return None

class Cat:
    def __init__(self, vfs):
        self.vfs = vfs

    def run(self, output, path, progress_callback=lambda *x: None):
        node = self.vfs.open(path)
        node.read(output, progress_callback=progress_callback)

class Get:
    def __init__(self, vfs, logger):
        self.vfs = vfs
        self.logger = logger

    def download_file(self, path, retry, dcb, ecb):
        self.logger.info('準備下載檔案 {}'.format(path))

        node_ready = False
        for i in range(retry):
            try:
                if i != 0:
                    self.logger.error('存取 {} 時發生錯誤，正在嘗試第 {} 次' \
                        .format(path, i + 1))
                node = self.vfs.open(path)
                node_ready = True
                break
            except (pycurl.error, Error) as err:
                self.logger.error(err)
        if not node_ready:
            return False

        if self.vfs.is_internal_link(node):
            return self.download_link(path, node, retry, dcb, ecb)
        elif self.vfs.is_regular(node):
            return self.download_regular(path, node, retry, dcb, ecb)
        elif self.vfs.is_directory(node):
            if not self.download_directory(path, node, retry, dcb, ecb):
                return False
            for child_name, child_node in node.list():
                child_path = pathlib.PurePosixPath(path) / child_name
                child_path = child_path.as_posix()
                if not self.download_file(child_path, retry, dcb, ecb):
                    return False
            return True
        else:
            assert False, '無法辨識的檔案格式'

    def download_link(self, path, node, retry, dcb, ecb):
        disk_path_object = pathlib.Path(path.lstrip('/'))
        disk_path_object = pathlib.Path(sanitize_filepath(str(disk_path_object)))
        disk_path = str(disk_path_object)
        if self.vfs.is_internal_link(node):
            link_target_path = str(pathlib.PurePath(node.read_link()))
        else:
            assert False

        if disk_path_object.is_symlink():
            existing_link_target_path = os.readlink(disk_path)
            if existing_link_target_path == link_target_path:
                self.logger.info('跳過已經存在且目標相同的符號連結 {}' \
                    .format(disk_path))
                return True

        download_ok = False
        for i in range(retry):
            try:
                if i != 0:
                    self.logger.error('無法建立符號連結 {}，正在嘗試第 {} 次' \
                        .format(disk_path, i + 1))
                try:
                    dcb(path, False, None, None, None)
                    disk_path_object.symlink_to(link_target_path)
                    dcb(path, True, None, None, None)
                    ecb(path)
                    download_ok = True
                    break
                except FileExistsError:
                    if disk_path_object.is_symlink():
                        disk_path_object.unlink()
                        dcb(path, False, None, None, None)
                        disk_path_object.symlink_to(link_target_path)
                        dcb(path, True, None, None, None)
                        ecb(path)
                        download_ok = True
                        break
            except IOError as err:
                ecb(path)
                self.logger.error(err)

        if not download_ok:
            return False

        return True

    def download_regular(self, path, node, retry, dcb, ecb):
        disk_path_object = pathlib.Path(path.lstrip('/'))
        disk_path_object = pathlib.Path(sanitize_filepath(str(disk_path_object)))
        
        def ccb(*args):
            return dcb(path, *args)

        def disk_path_object_open(mode):
            while True:
                try:
                    nonlocal disk_path_object
                    return disk_path_object.open(mode)
                except IOError as err:
                    if err.errno != errno.ENAMETOOLONG:
                        raise err
                    disk_path_object = disk_path_object.parent / \
                        (disk_path_object.stem[:-1] + disk_path_object.suffix)
                    self.logger.info('指定的檔案名稱太長，正在嘗試改用 {}' \
                        .format(str(disk_path_object)))

        download_ok = False
        for i in range(retry):
            try:
                if i != 0:
                    self.logger.error('下載檔案 {} 時發生錯誤，正在嘗試第 {} 次' \
                        .format(path, i + 1))
                disk_file_opened = False
                disk_file_read_opened = False
                try:
                    disk_file = disk_path_object_open('xb')
                    disk_file_opened = True
                except FileExistsError:
                    if disk_path_object.is_file() and \
                        disk_path_object.stat().st_size == node.size():
                        if node.local:
                            disk_file_read = disk_path_object_open('rb')
                            disk_file_read_opened = True
                            disk_file_content = disk_file_read.read()
                            disk_file_read.close()
                            ceiba_file_output = io.BytesIO()
                            node.read(ceiba_file_output)
                            ceiba_file_content = ceiba_file_output.getvalue()
                            if disk_file_content == ceiba_file_content:
                                self.logger.info(
                                    '跳過已經存在且內容相同的檔案 {}' \
                                    .format(str(disk_path_object)))
                                download_ok = True
                                break
                            else:
                                disk_file = disk_path_object_open('wb')
                                disk_file_opened = True
                        else:
                            self.logger.info('跳過已經存在且大小相同的檔案 {}' \
                                .format(str(disk_path_object)))
                            download_ok = True
                            break
                    else:
                        disk_file = disk_path_object_open('wb')
                        disk_file_opened = True
                node.read(disk_file, progress_callback=ccb)
                disk_file.close()
                ecb(path)
                download_ok = True
                break
            except (pycurl.error, Error, IOError) as err:
                self.logger.error(err)
                if disk_file_opened:
                    disk_file.close()
                if disk_file_read_opened:
                    disk_file_read.close()

        return download_ok

    def download_directory(self, path, node, retry, dcb, ecb):
        disk_path_object = pathlib.Path(path.lstrip('/'))
        disk_path_object = pathlib.Path(sanitize_filepath(str(disk_path_object)))
        if disk_path_object.is_dir():
            self.logger.info('跳過已經存在的資料夾 {}' \
                .format(str(disk_path_object)))
            return True

        download_ok = False
        for i in range(retry):
            try:
                if i != 0:
                    self.logger.error('無法建立資料夾 {}，正在嘗試第 {} 次' \
                        .format(str(disk_path_object), i + 1))
                dcb(path, False, None, None, None)
                disk_path_object.mkdir(parents=True, exist_ok=False)
                dcb(path, True, None, None, None)
                ecb(path)
                download_ok = True
                break
            except IOError as err:
                ecb(path)
                self.logger.error(err)

        if not download_ok:
            return False

        return True

    def run(self, path, retry=3,
        download_progress_callback=lambda *x: None,
        end_download_callback=lambda *x: None):
        return self.download_file(path, retry + 1,
            download_progress_callback, end_download_callback)

class Ls:
    def __init__(self, vfs, details=False, recursive=False):
        self.vfs = vfs
        self.details = details
        self.recursive = recursive

    def print_file(self, output, path, recursive):
        node = self.vfs.open(path)
        if self.vfs.is_internal_link(node):
            self.print_internal_link(output, path, node)
        elif self.vfs.is_regular(node):
            self.print_regular(output, path)
        elif self.vfs.is_directory(node):
            self.print_directory(output, path)
            for child_name, child_node in node.list():
                child_path = pathlib.PurePosixPath(path) / child_name
                child_path = child_path.as_posix()
                if not recursive and self.vfs.is_directory(child_node):
                    self.print_directory(output, child_path)
                else:
                    self.print_file(output, child_path, recursive)
        else:
            assert False, '無法辨識的檔案格式'

    def print_internal_link(self, output, path, node):
        if self.details:
            output.write('連結       {} -> {}\n'.format(path, node.read_link()))
        else:
            output.write(path + '\n')

    def print_regular(self, output, path):
        if self.details:
            output.write('普通檔案   {}\n'.format(path))
        else:
            output.write(path + '\n')

    def print_directory(self, output, path):
        if self.details:
            output.write('資料夾     {}\n'.format(path))
        else:
            output.write(path + '\n')

    def run(self, output, path):
        self.print_file(output, path, self.recursive)
