"""NewestFirstFileHandler：最新日志写在文件开头的回归测试。"""
import logging
import time

import pytest

from core.newest_first_log import NewestFirstFileHandler


def make_record(msg, level=logging.INFO):
    return logging.LogRecord('wifi_tray', level, __file__, 1, msg, None, None)


def read(path):
    return path.read_text(encoding='utf-8')


@pytest.fixture
def handler(tmp_path):
    h = NewestFirstFileHandler(tmp_path / 'tray_app.log', max_bytes=64 * 1024,
                               flush_interval=0.05)
    h.setFormatter(logging.Formatter('%(message)s'))
    try:
        yield h
    finally:
        h.close()


def test_newest_line_first(handler, tmp_path):
    handler.emit(make_record('first'))
    handler.emit(make_record('second'))
    handler._flush()
    lines = read(tmp_path / 'tray_app.log').splitlines()
    assert lines[0] == 'second'
    assert lines[1] == 'first'


def test_inherits_existing_file_content(tmp_path):
    log_file = tmp_path / 'tray_app.log'
    log_file.write_text('old-1\nold-2\n', encoding='utf-8')
    h = NewestFirstFileHandler(log_file, flush_interval=0.05)
    h.setFormatter(logging.Formatter('%(message)s'))
    try:
        h.emit(make_record('new-entry'))
        h._flush()
    finally:
        h.close()
    lines = read(log_file).splitlines()
    assert lines[0] == 'new-entry'
    assert lines[-2:] == ['old-1', 'old-2']


def test_multiline_record_keeps_block_order(handler, tmp_path):
    handler.emit(make_record('trace start\n  line A\n  line B'))
    handler.emit(make_record('after'))
    handler._flush()
    lines = read(tmp_path / 'tray_app.log').splitlines()
    assert lines[0] == 'after'
    assert lines[1:4] == ['trace start', '  line A', '  line B']


def test_trim_drops_oldest_lines(handler, tmp_path):
    # 每行约 20 字节，max_bytes=64KB：写入超量后只保留最新部分
    total = 5000
    for i in range(total):
        handler.emit(make_record(f'line-{i:04d}-aaaaaaaaaaaa'))
    handler._flush()
    content = read(tmp_path / 'tray_app.log')
    assert len(content.encode('utf-8')) <= 64 * 1024
    lines = content.splitlines()
    assert lines[0] == f'line-{total - 1:04d}-aaaaaaaaaaaa'
    assert 'line-0000-aaaaaaaaaaaa' not in content


def test_close_flushes_pending(tmp_path):
    h = NewestFirstFileHandler(tmp_path / 'tray_app.log', flush_interval=3600)
    h.setFormatter(logging.Formatter('%(message)s'))
    h.emit(make_record('pending-line'))
    h.close()
    assert 'pending-line' in read(tmp_path / 'tray_app.log')


def test_background_flush_writes_without_manual_call(handler, tmp_path):
    handler.emit(make_record('async-entry'))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if (tmp_path / 'tray_app.log').exists() and 'async-entry' in read(tmp_path / 'tray_app.log'):
            break
        time.sleep(0.05)
    assert 'async-entry' in read(tmp_path / 'tray_app.log')


def test_logger_integration(handler, tmp_path):
    logger = logging.getLogger('newest-first-test')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info('hello-integration')
        handler._flush()
        content = read(tmp_path / 'tray_app.log')
        assert 'hello-integration' in content
    finally:
        logger.removeHandler(handler)


def test_total_bytes_matches_content_after_trim(tmp_path):
    """增量字节计数与实际内容一致（trim 后仍守恒）。"""
    handler = NewestFirstFileHandler(tmp_path / 'tray_app.log', max_bytes=300,
                                     flush_interval=0)
    logger = logging.getLogger('byte-accounting-test')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        for i in range(50):
            logger.debug('line-%02d %s', i, 'x' * 20)
        # 同步 flush（与后台 flush 线程共用 io/内容锁），确保 trim 已执行
        handler._flush()
        expected = sum(len(l.encode('utf-8', 'replace')) + 1 for l in handler._lines)
        assert handler._total_bytes == expected
        assert handler._total_bytes <= 300
    finally:
        logger.removeHandler(handler)
        handler.close()
