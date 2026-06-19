"""鱼类图鉴面板 — 稀有度筛选、搜索、卡片网格与 ImageCache 联动。"""

from PySide6.QtCore import QEasingCurve, QTimer, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.cache import ImageCache
from gui.theme import (
    APP_COLORS,
    RARITY_META,
    RARITY_ORDER,
    add_shadow,
    line_edit_stylesheet,
    panel_stylesheet,
    primary_button_stylesheet,
    scroll_area_stylesheet,
    secondary_button_stylesheet,
)


class DexPanel(QFrame):
    def __init__(self, variant="elevated", parent=None):
        super().__init__(parent)
        self.setProperty("variant", variant)
        self.setStyleSheet(panel_stylesheet())
        add_shadow(self, blur=20, alpha=86, offset=(0, 8))


class FilterChip(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(secondary_button_stylesheet())


class FishCard(QFrame):
    def __init__(self, name, data, parent=None):
        super().__init__(parent)
        self.name = name
        self.data = data
        self.caught_count = int(data.get("caught_count", 0) or 0)
        self.show_all_mode = False
        self.pixmap = QPixmap()
        self.gray_pixmap = QPixmap()
        self.hover_value = 0.0
        self._last_state_signature = None

        self.setObjectName("fishCard")
        self.setFixedSize(292, 356)
        self.setAttribute(Qt.WA_Hover, True)
        self.setMouseTracking(True)

        self.shadow_effect = add_shadow(self, blur=18, alpha=54, offset=(0, 8))

        self.hover_anim = QVariantAnimation(self)
        self.hover_anim.setDuration(180)
        self.hover_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.hover_anim.valueChanged.connect(self._apply_hover_value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QFrame(self)
        self.header.setFixedHeight(180)
        header_layout = QVBoxLayout(self.header)
        header_layout.setContentsMargins(16, 14, 16, 12)
        header_layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self.type_label = QLabel("鱼")
        self.type_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px; font-weight: 800;")
        top_row.addWidget(self.type_label)
        top_row.addStretch()

        self.count_badge = QLabel("x0")
        self.count_badge.setAlignment(Qt.AlignCenter)
        self.count_badge.setFixedSize(58, 32)
        self.count_badge.setStyleSheet(
            """
            background-color: rgba(4, 12, 22, 0.72);
            color: #FFFFFF;
            border-radius: 16px;
            font-size: 14px;
            font-weight: 900;
            """
        )
        top_row.addWidget(self.count_badge)
        header_layout.addLayout(top_row)

        info_row = QHBoxLayout()
        info_row.setSpacing(14)

        self.ring = QFrame(self.header)
        self.ring.setFixedSize(122, 122)
        self.ring.setStyleSheet("background: transparent; border: none;")

        ring_layout = QVBoxLayout(self.ring)
        ring_layout.setContentsMargins(0, 0, 0, 0)
        ring_layout.setSpacing(0)

        self.ring_disc = QLabel(self.ring)
        self.ring_disc.setFixedSize(122, 122)
        self.ring_disc.setAlignment(Qt.AlignCenter)
        self.ring_disc.setStyleSheet(
            """
            border-radius: 61px;
            background-color: rgba(255, 255, 255, 0.06);
            border: 6px solid rgba(255, 255, 255, 0.08);
            """
        )
        ring_layout.addWidget(self.ring_disc)

        self.image_label = QLabel(self.ring_disc)
        self.image_label.setGeometry(15, 15, 92, 92)
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background: transparent; border: none;")
        info_row.addWidget(self.ring, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(8)
        text_col.addStretch()

        self.name_label = QLabel(name)
        self.name_label.setWordWrap(True)
        self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.name_label.setMinimumHeight(78)
        self.name_label.setStyleSheet("background: transparent; border: none; color: #FFFFFF; font-size: 17px; font-weight: 900;")
        text_col.addWidget(self.name_label)

        self.meta_label = QLabel("")
        self.meta_label.setWordWrap(True)
        self.meta_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px; font-weight: 600;")
        text_col.addWidget(self.meta_label)

        text_col.addStretch()
        info_row.addLayout(text_col, 1)
        header_layout.addLayout(info_row, 1)
        layout.addWidget(self.header)

        self.footer = QFrame(self)
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(16, 14, 16, 16)
        footer_layout.setSpacing(10)

        self.status_title = QLabel("图鉴记录")
        self.status_title.setStyleSheet(
            f"""
            background-color: rgba(255, 255, 255, 0.05);
            border: none;
            border-radius: 11px;
            color: {APP_COLORS["text"]};
            font-size: 12px;
            font-weight: 800;
            padding: 6px 10px;
            """
        )
        footer_layout.addWidget(self.status_title, 0, Qt.AlignLeft)

        self.desc_label = QLabel()
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px; font-weight: 600;")
        footer_layout.addWidget(self.desc_label)

        self.time_label = QLabel()
        self.time_label.setWordWrap(True)
        self.time_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;")
        footer_layout.addWidget(self.time_label)
        layout.addWidget(self.footer, 1)

        self.refresh_card()

    def _make_state_signature(self, show_all_mode, fish_data):
        return (
            bool(show_all_mode),
            int(fish_data.get("caught_count", 0) or 0),
            int(fish_data.get("max_weight", 0) or 0),
            fish_data.get("rarity", ""),
            fish_data.get("image_path", ""),
            fish_data.get("first_caught_at", ""),
            fish_data.get("last_caught_at", ""),
        )

    def enterEvent(self, event):
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def _animate_hover(self, target):
        self.hover_anim.stop()
        self.hover_anim.setStartValue(self.hover_value)
        self.hover_anim.setEndValue(target)
        self.hover_anim.start()

    def _apply_hover_value(self, value):
        self.hover_value = float(value)
        self._apply_styles()

    def set_image(self, pixmap):
        self.pixmap = pixmap or QPixmap()
        if self.pixmap.isNull():
            self.gray_pixmap = QPixmap()
            self.image_label.clear()
            return

        scaled = self.pixmap.scaled(92, 92, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.pixmap = scaled
        image = scaled.toImage().convertToFormat(scaled.toImage().Format.Format_Grayscale8)
        self.gray_pixmap = QPixmap.fromImage(image)
        self._apply_pixmap()

    def _apply_pixmap(self):
        if self.pixmap.isNull():
            self.image_label.clear()
            return
        if self.show_all_mode or self.caught_count > 0 or self.gray_pixmap.isNull():
            self.image_label.setPixmap(self.pixmap)
        else:
            self.image_label.setPixmap(self.gray_pixmap)

    def update_state(self, show_all_mode, fish_data):
        signature = self._make_state_signature(show_all_mode, fish_data)
        if signature == self._last_state_signature:
            return False

        self.show_all_mode = show_all_mode
        self.data = fish_data
        self.caught_count = int(fish_data.get("caught_count", 0))
        self.refresh_card()
        return True

    def _apply_styles(self):
        rarity = self.data.get("rarity", "未知稀有度")
        meta = RARITY_META.get(rarity, RARITY_META["未知稀有度"])
        accent = QColor(meta["color"])
        is_unlocked = self.show_all_mode or self.caught_count > 0
        hover_ratio = self.hover_value

        border_alpha = 36 + int(hover_ratio * 40)
        shadow_alpha = 44 + int(hover_ratio * 70)
        shadow_color = QColor(meta["color"] if is_unlocked else "#6F8196")
        shadow_color.setAlpha(shadow_alpha)
        self.shadow_effect.setBlurRadius(18 + hover_ratio * 18)
        self.shadow_effect.setOffset(0, 8 + hover_ratio * 4)
        self.shadow_effect.setColor(shadow_color)

        shell_bg_alpha = 210 + int(hover_ratio * 12)
        header_accent_alpha = 150 if is_unlocked else 34
        header_accent_alpha += int(hover_ratio * 28)
        header_glow_alpha = 42 + int(hover_ratio * 22)
        border_color = QColor(111, 145, 182, border_alpha)

        self.setStyleSheet(
            f"""
            QFrame#fishCard {{
                background-color: rgba(17, 28, 43, {shell_bg_alpha});
                border: 1px solid rgba({border_color.red()}, {border_color.green()}, {border_color.blue()}, {border_color.alpha()});
                border-radius: 28px;
            }}
            """
        )

        self.header.setStyleSheet(
            f"""
            background-color: transparent;
            border: none;
            border-top-left-radius: 28px;
            border-top-right-radius: 28px;
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
            background-image: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 rgba(8, 17, 28, 242),
                stop:0.45 rgba(14, 24, 37, 236),
                stop:0.72 rgba(13, 24, 38, 232),
                stop:1 rgba({accent.red()}, {accent.green()}, {accent.blue()}, {header_accent_alpha}));
            """
        )

        self.ring_disc.setStyleSheet(
            f"""
            border-radius: 61px;
            background-color: rgba(255, 255, 255, 0.05);
            border: 6px solid rgba({accent.red()}, {accent.green()}, {accent.blue()}, {255 if is_unlocked else 84});
            """
        )

        self.footer.setStyleSheet(
            f"""
            background-color: rgba(228, 239, 250, {34 + int(hover_ratio * 12)});
            border: none;
            border-top: 1px solid rgba(255, 255, 255, {20 + int(hover_ratio * 10)});
            border-top-left-radius: 0px;
            border-top-right-radius: 0px;
            border-bottom-left-radius: 28px;
            border-bottom-right-radius: 28px;
            """
        )

        self.status_title.setStyleSheet(
            f"""
            background-color: rgba(255, 255, 255, {12 + int(hover_ratio * 8)});
            border: none;
            border-radius: 11px;
            color: {APP_COLORS["text"]};
            font-size: 12px;
            font-weight: 800;
            padding: 6px 10px;
            """
        )

        self.meta_label.setStyleSheet(
            f"""
            background: transparent;
            border: none;
            color: {APP_COLORS["accent_soft"] if is_unlocked else APP_COLORS["text_dim"]};
            font-size: 12px;
            font-weight: 700;
            """
        )

        self.type_label.setStyleSheet(
            f"""
            background: transparent;
            border: none;
            color: rgba(255, 255, 255, {152 + int(hover_ratio * 28)});
            font-size: 12px;
            font-weight: 800;
            """
        )

        self._apply_pixmap()

    def refresh_card(self):
        self._last_state_signature = self._make_state_signature(self.show_all_mode, self.data)
        rarity = self.data.get("rarity", "未知稀有度")
        meta = RARITY_META.get(rarity, RARITY_META["未知稀有度"])
        is_unlocked = self.show_all_mode or self.caught_count > 0

        self.count_badge.setVisible(not self.show_all_mode)
        self.count_badge.setText(f"x{self.caught_count}")
        self.name_label.setText(self.name)
        self.name_label.setStyleSheet("background: transparent; border: none; color: #FFFFFF; font-size: 17px; font-weight: 900;")
        self.meta_label.setText(f"{meta['label']} · {'已解锁' if self.caught_count > 0 else '未获取'}")

        if self.show_all_mode:
            self.desc_label.setText("全部鱼类预览模式已开启，当前卡片保留原始主题颜色，用于完整查看所有鱼类外观。")
            self.time_label.setText("当前模式不显示累计钓起数量。")
        elif self.caught_count > 0:
            self.desc_label.setText("该目标已纳入自动钓鱼总记录，可结合捕获次数与最大重量持续观察收藏进度。")
            self.time_label.setText(f"累计钓起 {self.caught_count} 条 · 最大重量 {self.data.get('max_weight', 0)} g")
        else:
            self.desc_label.setText("尚未获取该鱼类，当前仅灰化鱼图，鱼名与文本依然保持清晰可读，便于查阅。")
            self.time_label.setText("累计钓起 0 条")

        self._apply_styles()


class EncyclopediaWidget(QWidget):
    CARD_WIDTH = 292
    GRID_GAP = 18

    def __init__(self, record_mgr):
        super().__init__()
        self.record_mgr = record_mgr
        self.cards = {}
        self.card_order = []
        self.show_all_mode = False
        self.visible_names = []
        self._last_layout_names = ()
        self._last_layout_columns = 0

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.timeout.connect(self.refresh_data)

        self.init_ui()

    def init_ui(self):
        self.setStyleSheet(panel_stylesheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(5)

        title = QLabel("图鉴记录")
        title.setProperty("role", "headline")
        title_col.addWidget(title)

        subtitle = QLabel("以鱼类资源目录作为主数据源，支持稀有度筛选、已解锁检索与完整图鉴预览。")
        subtitle.setProperty("role", "subtle")
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)

        self.toggle_mode_btn = QPushButton("切换为查看全部鱼")
        self.toggle_mode_btn.setCheckable(True)
        self.toggle_mode_btn.setFocusPolicy(Qt.NoFocus)
        self.toggle_mode_btn.setStyleSheet(primary_button_stylesheet())
        self.toggle_mode_btn.clicked.connect(self._toggle_mode)
        header.addWidget(self.toggle_mode_btn)
        layout.addLayout(header)

        self._build_summary(layout)
        self._build_filters(layout)
        self._build_grid(layout)
        self.refresh_data()

    def _build_summary(self, parent_layout):
        row = QHBoxLayout()
        row.setSpacing(14)
        self.summary_progress = self._summary_card("图鉴解锁", "0/0", "已获取种类")
        self.summary_gold = self._summary_card("高稀有收藏", "0", "金色捕获总数")
        self.summary_latest = self._summary_card("最近更新", "--", "暂无收录记录")
        row.addWidget(self.summary_progress)
        row.addWidget(self.summary_gold)
        row.addWidget(self.summary_latest)
        parent_layout.addLayout(row)

    def _summary_card(self, title, value, note):
        panel = DexPanel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 13px; font-weight: 700;")
        layout.addWidget(title_label)

        value_label = QLabel(value)
        value_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 24px; font-weight: 900;")
        layout.addWidget(value_label)

        note_label = QLabel(note)
        note_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;")
        layout.addWidget(note_label)

        panel.value_label = value_label
        panel.note_label = note_label
        return panel

    def _build_filters(self, parent_layout):
        panel = DexPanel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        top = QHBoxLayout()
        label = QLabel("分类筛选")
        label.setProperty("role", "section")
        top.addWidget(label)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索鱼类名称")
        self.search_edit.setStyleSheet(line_edit_stylesheet())
        self.search_edit.textChanged.connect(self._schedule_refresh)
        top.addWidget(self.search_edit, 1)

        self.unlocked_only_btn = FilterChip("仅看已解锁")
        self.unlocked_only_btn.clicked.connect(self.refresh_data)
        top.addWidget(self.unlocked_only_btn)
        layout.addLayout(top)

        chip_row = QHBoxLayout()
        chip_row.setSpacing(10)
        self.rarity_group = QButtonGroup(self)
        self.rarity_group.setExclusive(True)

        for rarity in ["全部"] + RARITY_ORDER:
            chip = FilterChip("全部鱼类" if rarity == "全部" else RARITY_META[rarity]["label"])
            chip.rarity_value = rarity
            chip.clicked.connect(self.refresh_data)
            self.rarity_group.addButton(chip)
            chip_row.addWidget(chip)
            if rarity == "全部":
                chip.setChecked(True)

        chip_row.addStretch()
        layout.addLayout(chip_row)
        parent_layout.addWidget(panel)

    def _build_grid(self, parent_layout):
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFocusPolicy(Qt.NoFocus)
        self.scroll_area.setStyleSheet(scroll_area_stylesheet())
        self.scroll_area.viewport().setStyleSheet("background: transparent;")

        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background: transparent;")
        self.grid_layout = QGridLayout(self.scroll_content)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setHorizontalSpacing(self.GRID_GAP)
        self.grid_layout.setVerticalSpacing(self.GRID_GAP)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self.scroll_area.setWidget(self.scroll_content)
        parent_layout.addWidget(self.scroll_area, 1)
        self._build_cards()

    def _build_cards(self):
        encyclopedia = self.record_mgr.get_encyclopedia()
        ordered = []
        for rarity in RARITY_ORDER:
            fishes = sorted((name, data) for name, data in encyclopedia.items() if data.get("rarity") == rarity)
            ordered.extend(fishes)

        cache = ImageCache.get_instance()
        cache.preload_many(ordered)

        for name, data in ordered:
            card = FishCard(name, data, self.scroll_content)
            card.hide()
            self.cards[name] = card
            self.card_order.append(name)
            cache.request_image(
                data.get("image_path", ""),
                name,
                data.get("rarity", ""),
                lambda _name, pixmap, card_ref=card: card_ref.set_image(pixmap),
            )

    def _toggle_mode(self):
        self.show_all_mode = self.toggle_mode_btn.isChecked()
        self.toggle_mode_btn.setText("切换为图鉴记录模式" if self.show_all_mode else "切换为查看全部鱼")
        if self.show_all_mode and self.unlocked_only_btn.isChecked():
            self.unlocked_only_btn.setChecked(False)
        self.refresh_data()

    def _schedule_refresh(self):
        self.refresh_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout_cards()

    def relayout_cards(self):
        width = max(1, self.scroll_area.viewport().width())
        columns = max(1, (width + self.GRID_GAP) // (self.CARD_WIDTH + self.GRID_GAP))
        layout_names = tuple(self.visible_names)
        if layout_names == self._last_layout_names and columns == self._last_layout_columns:
            return

        self._last_layout_names = layout_names
        self._last_layout_columns = columns
        self.scroll_content.setUpdatesEnabled(False)
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().hide()

        for index, name in enumerate(self.visible_names):
            card = self.cards[name]
            row = index // columns
            col = index % columns
            self.grid_layout.addWidget(card, row, col)
            card.show()
        self.scroll_content.setUpdatesEnabled(True)
        self.scroll_content.update()

    def refresh_data(self):
        encyclopedia = self.record_mgr.get_encyclopedia()
        keyword = self.search_edit.text().strip().lower()
        checked = next((button for button in self.rarity_group.buttons() if button.isChecked()), None)
        rarity_filter = checked.rarity_value if checked else "全部"
        unlocked_only = self.unlocked_only_btn.isChecked() and not self.show_all_mode

        self.unlocked_only_btn.setEnabled(not self.show_all_mode)

        unlocked_count = 0
        gold_caught = 0
        latest_name = "--"
        latest_time = ""
        visible_names = []

        self.scroll_content.setUpdatesEnabled(False)
        for name in self.card_order:
            data = encyclopedia.get(name, self.cards[name].data)
            rarity = data.get("rarity", "未知稀有度")
            caught_count = int(data.get("caught_count", 0))

            if caught_count > 0:
                unlocked_count += 1
            if rarity == "金色稀有度":
                gold_caught += caught_count
            if data.get("last_caught_at", "") > latest_time:
                latest_time = data.get("last_caught_at", "")
                latest_name = name

            matches_keyword = not keyword or keyword in name.lower()
            matches_rarity = rarity_filter == "全部" or rarity == rarity_filter
            matches_unlock = (not unlocked_only) or caught_count > 0

            card = self.cards[name]
            card.update_state(self.show_all_mode, data)
            if matches_keyword and matches_rarity and matches_unlock:
                visible_names.append(name)
        self.scroll_content.setUpdatesEnabled(True)

        self.visible_names = visible_names
        self.summary_progress.value_label.setText(f"{unlocked_count}/{len(encyclopedia)}")
        self.summary_progress.note_label.setText("已获取种类" if not self.show_all_mode else "完整图鉴总量")
        self.summary_gold.value_label.setText(str(gold_caught))
        self.summary_gold.note_label.setText("金色捕获总数" if not self.show_all_mode else "全图鉴模式下仅作参考")
        self.summary_latest.value_label.setText(latest_name if latest_name else "--")
        self.summary_latest.note_label.setText(latest_time if latest_time else "暂无收录记录")

        self.relayout_cards()
