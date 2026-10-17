"""Read bounded, text-only Skill files without following links or executing code."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import stat
from urllib.parse import unquote, urlsplit


class PackageError(ValueError):
    def __init__(self, code, path, message):
        self.issue = {'code': code, 'path': str(path), 'message': message}
        super().__init__(message)


def fail(code, path, message):
    raise PackageError(code, path, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('nonfinite JSON number')

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('overflow JSON number')
        return number

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def read_files(directory):
    root = Path(directory).absolute()
    if _linked(root.lstat()):
        fail('UNSAFE_PATH', '.', '包目录不能是符号链接或重解析点。')
    if not root.is_dir():
        fail('INVALID_DIRECTORY', '.', '需要一个存在的 Skill 包目录。')
    files, total, entries, names = {}, 0, 0, set()

    def visit(folder, depth):
        nonlocal total, entries
        if depth > 6:
            fail('PACKAGE_LIMIT', '.', '包目录层级超过6层。')
        for child in sorted(folder.iterdir(), key=lambda p: p.name):
            entries += 1
            rel = child.relative_to(root).as_posix()
            info = child.lstat()
            if entries > 128:
                fail('PACKAGE_LIMIT', '.', '包内目录及文件数量超过限制。')
            if _linked(info):
                fail('UNSAFE_PATH', rel, '不能加载符号链接或重解析点。')
            if ':' in rel or '\\' in rel or any(ord(c) < 32 for c in rel):
                fail('UNSAFE_PATH', rel, '文件路径包含不支持的字符。')
            if rel.casefold() in names:
                fail('UNSAFE_PATH', rel, '文件路径不能仅以大小写区分。')
            names.add(rel.casefold())
            if stat.S_ISDIR(info.st_mode):
                if depth == 0 and child.name not in ('schemas', 'references', 'assets'):
                    fail('UNSUPPORTED_FILE', rel, '仅支持 schemas、references 和 assets 资源目录。')
                visit(child, depth + 1)
                continue
            if not stat.S_ISREG(info.st_mode):
                fail('UNSAFE_PATH', rel, '只接受普通文本文件。')
            if child.suffix not in ('.md', '.txt', '.json') or (depth == 0 and child.name not in ('SKILL.md', 'descriptor.json')):
                fail('UNSUPPORTED_FILE', rel, '包中不允许可执行文件或未声明的根文件。')
            if len(files) >= 64 or info.st_size > 256 * 1024:
                fail('PACKAGE_LIMIT', rel, '文件数量或单个文件大小超过限制。')
            with child.open('rb') as stream:
                data = stream.read(256 * 1024 + 1)
            total += len(data)
            if len(data) > 256 * 1024 or total > 1024 * 1024:
                fail('PACKAGE_LIMIT', rel, '文件或整个包超过大小限制。')
            try:
                text = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
                if '\0' in text:
                    raise ValueError('binary')
            except (UnicodeError, ValueError):
                fail('INVALID_TEXT', rel, '文件必须为不含空字符的 UTF-8 文本。')
            files[rel] = text

    visit(root, 0)
    for required in ('SKILL.md', 'descriptor.json'):
        if required not in files:
            fail('MISSING_FILE', required, '缺少必需文件。')
    return files


def local_reference(source, target, files):
    """Return an existing package path; references never open another file."""
    parsed = urlsplit(target)
    decoded = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or parsed.query or decoded.startswith('/') or '\\' in decoded or ':' in decoded:
        fail('INVALID_REFERENCE', source, '引用必须是包内相对路径。')
    parts = source.split('/')[:-1]
    for part in decoded.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                fail('INVALID_REFERENCE', source, '引用越出了包目录。')
            parts.pop()
        else:
            parts.append(part)
    path = '/'.join(parts) if decoded else source
    if path not in files:
        fail('INVALID_REFERENCE', source, '引用的包内文件不存在。')
    return path


def fingerprint(files, descriptor):
    manifest = []
    for path, text in sorted(files.items()):
        if path == 'descriptor.json':
            text = canonical({k: v for k, v in descriptor.items() if k != 'hash'})
        manifest.append({'path': path, 'hash': hashlib.sha256(text.encode('utf-8')).hexdigest()})
    value = [[item['path'], item['hash']] for item in manifest]
    return 'sha256:' + hashlib.sha256(canonical(value).encode('utf-8')).hexdigest(), manifest
