def handle_files_read(payload):
    rel = payload.get('path')
    if not rel:
        return _err('path is required')
    full = _safe_path(rel)
    try:
        mode = os.stat(full).st_mode
    except OSError as e:
        if e.errno == errno.ENOENT:
            result = _err('file not found: %s' % rel)
            result['code'] = 'not_found'
            return result
        return _err('read failed: %s' % str(e))
    if not stat.S_ISREG(mode):
        return _err('not a regular file: %s' % rel)
    try:
        if payload.get('format') == 'sha256':
            digest = hashlib.sha256()
            with open(full, 'rb') as f:
                for block in iter(lambda: f.read(65536), b''):
                    digest.update(block)
            return {'sha256': digest.hexdigest()}
        with codecs.open(full, 'r', 'utf-8') as f:
            content = f.read()
        return {'content': content}
    except Exception as e:
        return _err('read failed: %s' % str(e))
