"""GUI 鱼类图鉴缩略图缓存。

单例 ImageCache 在首次加载时将图片缩放到 TARGET_SIZE，避免列表滚动时
重复解码大图并占用过多显存。
"""

import os

from PySide6.QtCore import QObject, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPixmap


class ImageCache(QObject):
    """图鉴/战绩界面共用的 QPixmap 缓存，按鱼类名称键控。"""

    _instance = None
    TARGET_SIZE = 128  # 缩略图最长边像素

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.vram_cache = {}  # name -> QPixmap（空 QPixmap 表示加载失败占位）

    def _load_pixmap(self, path, name):
        """加载并缩放图片；失败时缓存空 QPixmap 防止反复读盘。"""
        if name in self.vram_cache:
            return self.vram_cache[name]

        if not path or not os.path.exists(path):
            self.vram_cache[name] = QPixmap()
            return self.vram_cache[name]

        reader = QImageReader(path)
        image_size = reader.size()
        if image_size.isValid():
            image_size.scale(QSize(self.TARGET_SIZE, self.TARGET_SIZE), Qt.KeepAspectRatio)
            reader.setScaledSize(image_size)
        image = reader.read()
        if image.isNull():
            image = QImage(path)
        if image.isNull():
            self.vram_cache[name] = QPixmap()
            return self.vram_cache[name]

        if image.width() > self.TARGET_SIZE or image.height() > self.TARGET_SIZE:
            image = image.scaled(self.TARGET_SIZE, self.TARGET_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pixmap = QPixmap.fromImage(image)
        self.vram_cache[name] = pixmap
        return pixmap

    def request_image(self, path, name, _rarity, callback):
        """异步风格接口：立即回调已缓存或刚加载的 pixmap（_rarity 保留供接口兼容）。"""
        callback(name, self._load_pixmap(path, name))

    def preload_many(self, fish_entries):
        """批量预热缓存，打开图鉴前调用以减少首屏闪烁。"""
        for name, data in fish_entries:
            self._load_pixmap(data.get("image_path", ""), name)
