"""最新日志置顶的文件日志 Handler。

传统 RotatingFileHandler 是追加模式，查看日志必须滚到文件末尾。
本 Handler 把最新日志写在文件开头：emit 只更新内存缓冲，后台线程
防抖聚合一小批后整体重写文件（临时文件 + os.replace 原子替换）。
文件总量按 max_bytes 从尾部截断（丢弃最旧的行），不再滚动备份。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path


class NewestFirstFileHandler(logging.Handler):
    def __init__(self, path, max_bytes=2 * 1024 * 1024, flush_interval=2.5,
                 encoding='utf-8'):
        super().__init__()
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.flush_interval = max(0.0, float(flush_interval))
        self.encoding = encoding
        self._lines: list[str] = []
        # 字节总量增量维护：emit 时累加、trim 时扣减，避免每次 flush
        # 对全部缓存行重新编码计数（O(n) 的纯 CPU 开销）
        self._total_bytes = 0
        self._lock = threading.RLock()      # 保护 _lines
        self._io_lock = threading.Lock()    # 串行化临时文件写入与替换
        self._dirty = threading.Event()
        self._closing = False
        self._load_existing()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name='log-newest-first')
        self._flush_thread.start()

    def _load_existing(self):
        """继承已有文件内容：旧内容整体沉底，新日志继续写在最前。"""
        try:
            if self.path.exists():
                text = self.path.read_text(encoding=self.encoding, errors='replace')
                self._lines = text.splitlines()
        except OSError:
            self._lines = []
        self._total_bytes = sum(
            len(line.encode(self.encoding, 'replace')) + 1 for line in self._lines)

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            # 多行记录（如 traceback）按行拆开整体插到最前，保持块内顺序
            new_lines = msg.splitlines()
            self._lines[0:0] = new_lines
            self._total_bytes += sum(
                len(line.encode(self.encoding, 'replace')) + 1 for line in new_lines)
        self._dirty.set()

    def _trim_locked(self):
        if self._total_bytes <= self.max_bytes:
            return
        sizes = [len(line.encode(self.encoding, 'replace')) + 1
                 for line in self._lines]
        used = 0
        keep = len(self._lines)
        for i, size in enumerate(sizes):
            used += size
            if used > self.max_bytes:
                keep = max(i, 1)  # 至少保留最新的 1 行
                break
        self._total_bytes -= sum(sizes[keep:])
        del self._lines[keep:]

    def _flush(self):
        with self._lock:
            self._trim_locked()
            content = '\n'.join(self._lines) + '\n' if self._lines else ''
        with self._io_lock:
            if not content:
                return
            tmp = self.path.with_name(self.path.name + '.tmp')
            tmp.write_text(content, encoding=self.encoding)
            os.replace(tmp, self.path)

    def _flush_loop(self):
        while not self._closing:
            self._dirty.wait()
            if self._closing:
                return
            # 抖动窗口：聚合同一时间的一批日志，避免逐条重写整个文件
            if self.flush_interval:
                time.sleep(self.flush_interval)
            self._dirty.clear()
            try:
                self._flush()
            except Exception:
                pass  # 落盘失败不能拖垮应用；内存里仍保留最新内容

    def close(self):
        self._closing = True
        self._dirty.set()
        self._flush_thread.join(timeout=2.0)
        try:
            self._flush()
        except Exception:
            pass
        super().close()
