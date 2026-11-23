"""WinForms PurpleShield Pilot HUD (紫盾快捷领航栏 + 极速改密/换绑 2FA 浮窗)."""

from __future__ import annotations

import ctypes
import time
from collections.abc import Callable

from src.crash_logger import log_exception
from src.totp_service import generate_strong_password, generate_totp, normalize_secret


# Supplementary-plane emoji are mapped onto BMP glyphs that GDI renders reliably;
# anything else above U+FFFF is dropped so the HUD can never show ``□`` tofu.
_GDI_EMOJI_FALLBACKS = {
    "\U0001F6E1": "◆",  # 🛡 shield
    "\U0001F4E7": "◆",  # 📧 e-mail
    "\U0001F511": "◆",  # 🔑 key
    "\U0001F4EE": "◆",  # 📮 postbox
    "\U0001F310": "◆",  # 🌐 globe
    "\U0001F4CB": "✓",  # 📋 clipboard
    "\U0001F4F1": "▸",  # 📱 mobile
    "\U0001F4BB": "▸",  # 💻 laptop
    "\U0001F4DD": "◆",  # 📝 memo
    "\U0001F6AB": "×",  # 🚫 prohibited
    "\U0001F4B3": "◆",  # 💳 credit card
    "\U0001F9F9": "↻",  # 🧹 broom
    "\U0001F504": "↻",  # 🔄 refresh
    "\U0001F916": "★",  # 🤖 robot
    "\U0001F538": "✦",  # 🔸 small diamond
    "\U0001F537": "◆",  # 🔷 large diamond
    "\U0001F4E5": "▸",  # 📥 inbox tray
    "\U0001F3B2": "✦",  # 🎲 dice
    "\U0001F4BE": "✓",  # 💾 floppy
    "\U0001F510": "◆",  # 🔐 locked key
    "\U0001F512": "◆",  # 🔒 lock
    "\U0001F513": "◆",  # 🔓 unlock
    "\U0001F691": "◆",  # 🚑 ambulance
    "\U0001F4DC": "◆",  # 📜 scroll
    "\U0001F30D": "◆",  # 🌍 globe
    "\U0001F382": "◆",  # 🎂 cake
    "\U0001F50D": "◆",  # 🔍 magnifier
    "\U0001F4CC": "◆",  # 📌 pin
    "\U0001F6A8": "⚠",  # 🚨 rotating light
    "\U0001F4A1": "✦",  # 💡 bulb
    "\U0001F525": "✦",  # 🔥 fire
    "\U0001F31F": "★",  # 🌟 star
    "\U0001F4E2": "◆",  # 📢 loudspeaker
    "\U0001F4E4": "▸",  # 📤 outbox tray
    "\U0001F4C4": "◆",  # 📄 page
    "\U0001F4C1": "◆",  # 📁 folder
    "\U0001F5C2": "◆",  # 🗂 card index
    "\U0001F5A5": "▸",  # 🖥 desktop
    "\U0001F5B1": "▸",  # 🖱 mouse
    "\U0001F6E0": "✦",  # 🛠 hammer & wrench
    "\U0001F527": "✦",  # 🔧 wrench
    "\U0001F528": "✦",  # 🔨 hammer
    "\U0001F680": "✦",  # 🚀 rocket
    "\U0001F3AF": "◆",  # 🎯 target
    "\U0001F4CA": "◆",  # 📊 chart
    "\U0001F4C8": "◆",  # 📈 chart up
    "\U0001F4C9": "◆",  # 📉 chart down
    "\U0001F9ED": "◆",  # 🧭 compass
    "\U0001F5DD": "◆",  # 🗝 old key
    "\U0001F6AA": "▸",  # 🚪 door
    "\U0001F441": "◆",  # 👁 eye
    "\U0001F4AC": "◆",  # 💬 speech balloon
    "\U0001F4A5": "⚠",  # 💥 collision
    "\U0001F6A7": "⚠",  # 🚧 construction
    "\U0001F6A6": "⚠",  # 🚦 vertical traffic light
    "\U0001F4A3": "⚠",  # 💣 bomb
    "\U0001F33F": "✦",  # 🌿 herb
    "\U0001F340": "✦",  # 🍀 four leaf clover
}


def _sanitize_gdi_text(text: str) -> str:
    """Return text GDI can render without ``□`` tofu boxes.

    WinForms' GDI text path renders supplementary-plane emoji (``ord(ch) > 0xFFFF``)
    and emoji variation selectors as empty boxes.  This helper strips the
    variation selectors / zero-width joiner / keycap combiner and maps common
    emoji onto clean BMP symbols (or drops them), keeping the high-contrast BMP
    glyphs such as ``⠿ ● ✦ ⚡ ◆ ▾ — × ✓ ⚠ ★ ▸ ↻`` untouched.
    """
    if not text:
        return ""
    out: list[str] = []
    for ch in str(text):
        code = ord(ch)
        if code in (0xFE0F, 0xFE0E, 0x200D, 0x20E3) or 0xFE00 <= code <= 0xFE0F:
            continue
        if code > 0xFFFF:
            out.append(_GDI_EMOJI_FALLBACKS.get(ch, ""))
            continue
        out.append(ch)
    cleaned = "".join(out)
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned.strip()


def _win32_drag_window(handle: int) -> None:
    try:
        user32 = ctypes.windll.user32
        user32.ReleaseCapture()
        user32.SendMessageW(ctypes.c_void_p(int(handle)), 0x00A1, 2, 0)
    except Exception:
        pass


def _rounded_path(width: int, height: int, radius: int, inset: int = 0):
    from System.Drawing.Drawing2D import GraphicsPath

    inset = max(0.0, float(inset))
    path = GraphicsPath()
    w = max(4.0, width - 2 * inset)
    h = max(4.0, height - 2 * inset)
    d = min(float(max(4, radius)), w, h)
    right = inset + w - d
    bottom = inset + h - d
    path.AddArc(inset, inset, d, d, 180, 90)
    path.AddArc(right, inset, d, d, 270, 90)
    path.AddArc(right, bottom, d, d, 0, 90)
    path.AddArc(inset, bottom, d, d, 90, 90)
    path.CloseFigure()
    return path


def _bind_rounded_shape(control, radius: int, border_color=None) -> None:
    """Clip a control to a rounded region and anti-alias its 1px border."""
    from System.Drawing import Pen, Region
    from System.Drawing.Drawing2D import SmoothingMode

    def clip(_sender, _event):
        if control.Width <= 2 or control.Height <= 2 or control.IsDisposed:
            return
        scale = max(1.0, float(control.DeviceDpi) / 96.0)
        path = _rounded_path(control.Width, control.Height, round(radius * scale))
        try:
            control.Region = Region(path)
        finally:
            path.Dispose()

    def paint(_sender, event):
        if border_color is None or control.Width <= 2 or control.Height <= 2 or control.IsDisposed:
            return
        scale = max(1.0, float(control.DeviceDpi) / 96.0)
        # Inset by 1.5px so the anti-aliased stroke never clips at the corners.
        path = _rounded_path(
            control.Width, control.Height, round(radius * scale), inset=1.5 * scale
        )
        pen = Pen(border_color, 1.5)
        try:
            event.Graphics.SmoothingMode = SmoothingMode.AntiAlias
            event.Graphics.DrawPath(pen, path)
        finally:
            path.Dispose()
            pen.Dispose()

    control.SizeChanged += clip
    control.Paint += paint
    clip(None, None)


def _enable_double_buffer(control) -> None:
    try:
        from System.Reflection import BindingFlags

        prop = control.GetType().GetProperty(
            "DoubleBuffered", BindingFlags.Instance | BindingFlags.NonPublic
        )
        prop.SetValue(control, True)
    except Exception:
        pass


def _enable_owner_paint(control) -> None:
    """Switch a Button to UserPaint so the Paint handler draws the whole pill."""
    try:
        from System.Reflection import BindingFlags
        from System.Windows.Forms import ControlStyles

        method = control.GetType().GetMethod(
            "SetStyle", BindingFlags.Instance | BindingFlags.NonPublic
        )
        method.Invoke(
            control,
            [
                ControlStyles.UserPaint
                | ControlStyles.AllPaintingInWmPaint
                | ControlStyles.OptimizedDoubleBuffer
                | ControlStyles.ResizeRedraw,
                True,
            ],
        )
    except Exception:
        pass


def _measure_text_width(text: str, font, padding: int = 0) -> int:
    try:
        from System.Drawing import Size
        from System.Windows.Forms import TextFormatFlags, TextRenderer

        measured = TextRenderer.MeasureText(
            text or " ", font, Size.Empty, TextFormatFlags.NoPadding
        )
        return max(1, int(measured.Width) + int(padding))
    except Exception:
        return max(1, round(len(text or " ") * 7 + padding))


def _fit_pill_width(button, *, min_width: int = 0, padding_x: int = 12) -> None:
    width = max(int(min_width), _measure_text_width(button.Text, button.Font, padding_x))
    if button.Width != width:
        button.Width = width


def _set_pill_colors(button, *, bg=None, hover=None, press=None, border=None, fg=None) -> None:
    hud = getattr(button, "_hud", None)
    if hud is None:
        return
    changed = False
    for key, value in (
        ("bg", bg),
        ("hover", hover),
        ("press", press),
        ("border", border),
        ("fg", fg),
    ):
        if value is not None and hud.get(key) != value:
            hud[key] = value
            changed = True
    if changed:
        if bg is not None:
            button.BackColor = bg
        if fg is not None:
            button.ForeColor = fg
        try:
            button.Invalidate()
        except Exception:
            pass


def _make_pill_button(
    text: str,
    *,
    bg,
    fg,
    border,
    hover,
    press,
    backdrop,
    min_width: int = 0,
    height: int = 30,
    padding_x: int = 12,
    bold: bool = False,
    font_size: float = 9.0,
    font_name: str = "Microsoft YaHei UI",
    radius: int = 6,
):
    """Create an owner-drawn rounded capsule button with crisp ClearType text.

    Returns a real ``System.Windows.Forms.Button`` so ``.Text`` / ``.Enabled`` /
    ``.Click`` / ``.PerformClick()`` keep working for every caller.
    """
    from System.Drawing import Color, Font, FontStyle, Pen, Rectangle, SolidBrush
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Windows.Forms import Button, Cursors, FlatStyle, Padding, TextFormatFlags, TextRenderer

    btn = Button()
    btn.Text = _sanitize_gdi_text(text)
    btn.AutoSize = False
    btn.Height = int(height)
    btn.FlatStyle = FlatStyle.Flat
    btn.FlatAppearance.BorderSize = 0
    btn.BackColor = bg
    btn.ForeColor = fg
    btn.Cursor = Cursors.Hand
    btn.TabStop = False
    btn.Font = Font(font_name, float(font_size), FontStyle.Bold if bold else FontStyle.Regular)
    btn.Margin = Padding(0, 0, 2, 0)
    btn._hud = {
        "bg": bg,
        "hover": hover,
        "press": press,
        "border": border,
        "fg": fg,
        "disabled_fg": Color.FromArgb(100, 116, 139),
        "backdrop": backdrop,
        "radius": radius,
    }
    btn._hud_hover = False
    btn._hud_pressed = False
    btn.Width = max(int(min_width), _measure_text_width(btn.Text, btn.Font, padding_x))

    def on_paint(_sender, event):
        # Paint handlers receive a fresh .NET proxy object, so Python attributes
        # set on ``btn`` must be read through the captured instance.
        hud = getattr(btn, "_hud", None)
        if hud is None or btn.Width <= 2 or btn.Height <= 2:
            return
        graphics = event.Graphics
        try:
            graphics.SmoothingMode = SmoothingMode.AntiAlias
            graphics.Clear(hud["backdrop"])
            if getattr(btn, "_hud_pressed", False):
                fill = hud["press"]
            elif getattr(btn, "_hud_hover", False):
                fill = hud["hover"]
            else:
                fill = hud["bg"]
            scale = max(1.0, float(btn.DeviceDpi) / 96.0)
            radius_px = round(hud["radius"] * scale)
            fill_path = _rounded_path(btn.Width, btn.Height, radius_px)
            border_path = _rounded_path(btn.Width, btn.Height, radius_px, inset=1.5 * scale)
            brush = SolidBrush(fill)
            pen = Pen(hud["border"], 1.5)
            try:
                graphics.FillPath(brush, fill_path)
                graphics.DrawPath(pen, border_path)
            finally:
                brush.Dispose()
                pen.Dispose()
                fill_path.Dispose()
                border_path.Dispose()
            text_color = hud["fg"] if btn.Enabled else hud["disabled_fg"]
            TextRenderer.DrawText(
                graphics,
                btn.Text,
                btn.Font,
                Rectangle(0, 0, btn.Width, btn.Height),
                text_color,
                TextFormatFlags.HorizontalCenter
                | TextFormatFlags.VerticalCenter
                | TextFormatFlags.SingleLine
                | TextFormatFlags.NoPadding,
            )
        except Exception:
            pass

    def on_enter(_sender, _event):
        btn._hud_hover = True
        btn.Invalidate()

    def on_leave(_sender, _event):
        btn._hud_hover = False
        btn._hud_pressed = False
        btn.Invalidate()

    def on_down(_sender, event):
        try:
            if str(event.Button) == "Left":
                btn._hud_pressed = True
                btn.Invalidate()
        except Exception:
            pass

    def on_up(_sender, _event):
        btn._hud_pressed = False
        btn.Invalidate()

    btn.Paint += on_paint
    btn.MouseEnter += on_enter
    btn.MouseLeave += on_leave
    btn.MouseDown += on_down
    btn.MouseUp += on_up
    btn.EnabledChanged += lambda _sender, _e: btn.Invalidate()
    btn.TextChanged += lambda _sender, _e: btn.Invalidate()
    _enable_owner_paint(btn)
    _enable_double_buffer(btn)
    return btn


def _make_dark_menu_renderer():
    """Build the ``_DarkMenuRenderer`` used by the HUD context menus."""
    from System.Windows.Forms import ToolStripProfessionalRenderer

    class _DarkMenuRenderer(ToolStripProfessionalRenderer):
        def __init__(self):
            self._dark_table = _make_dark_color_table()
            super().__init__(self._dark_table)
            self.RoundedEdges = False

        @property
        def ColorTable(self):
            return self._dark_table

        def OnRenderToolStripBackground(self, event):
            from System.Drawing import Color

            try:
                event.Graphics.Clear(Color.FromArgb(15, 23, 42))
            except Exception:
                super().OnRenderToolStripBackground(event)

        def OnRenderToolStripBorder(self, event):
            from System.Drawing import Color, Pen
            from System.Drawing.Drawing2D import SmoothingMode

            try:
                event.Graphics.SmoothingMode = SmoothingMode.AntiAlias
                pen = Pen(Color.FromArgb(79, 70, 229), 1.0)
                try:
                    rect = event.ToolStrip.ClientRectangle
                    event.Graphics.DrawRectangle(
                        pen, rect.X, rect.Y, max(1, rect.Width - 1), max(1, rect.Height - 1)
                    )
                finally:
                    pen.Dispose()
            except Exception:
                super().OnRenderToolStripBorder(event)

    return _DarkMenuRenderer()


def _make_dark_color_table():
    """Build the slate/indigo professional colour table for the HUD menus."""
    from System.Drawing import Color
    from System.Windows.Forms import ProfessionalColorTable

    class _DarkColorTable(ProfessionalColorTable):
        @property
        def ToolStripDropDownBackground(self):
            return Color.FromArgb(15, 23, 42)

        @property
        def MenuBorder(self):
            return Color.FromArgb(79, 70, 229)

        @property
        def MenuItemBorder(self):
            return Color.FromArgb(79, 70, 229)

        @property
        def MenuItemSelected(self):
            return Color.FromArgb(30, 58, 138)

        @property
        def MenuItemSelectedGradientBegin(self):
            return Color.FromArgb(30, 58, 138)

        @property
        def MenuItemSelectedGradientEnd(self):
            return Color.FromArgb(51, 65, 85)

        @property
        def MenuItemPressedGradientBegin(self):
            return Color.FromArgb(51, 65, 85)

        @property
        def MenuItemPressedGradientEnd(self):
            return Color.FromArgb(30, 41, 59)

        @property
        def ImageMarginGradientBegin(self):
            return Color.FromArgb(15, 23, 42)

        @property
        def ImageMarginGradientMiddle(self):
            return Color.FromArgb(15, 23, 42)

        @property
        def ImageMarginGradientEnd(self):
            return Color.FromArgb(15, 23, 42)

        @property
        def SeparatorDark(self):
            return Color.FromArgb(51, 65, 85)

        @property
        def SeparatorLight(self):
            return Color.FromArgb(30, 41, 59)

        @property
        def ToolStripBorder(self):
            return Color.FromArgb(79, 70, 229)

        @property
        def ToolStripGradientBegin(self):
            return Color.FromArgb(15, 23, 42)

        @property
        def ToolStripGradientMiddle(self):
            return Color.FromArgb(15, 23, 42)

        @property
        def ToolStripGradientEnd(self):
            return Color.FromArgb(15, 23, 42)

    return _DarkColorTable()


def _paint_menu_item(item, backdrop) -> None:
    """Owner-paint a menu item with the dark theme (hover aware, tofu free)."""
    from System.Drawing import Color, Rectangle
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Windows.Forms import TextFormatFlags, TextRenderer

    def on_paint(sender, event):
        try:
            graphics = event.Graphics
            graphics.SmoothingMode = SmoothingMode.AntiAlias
            hovered = bool(getattr(item, "_hud_hover", False) or getattr(sender, "Selected", False))
            graphics.Clear(Color.FromArgb(30, 58, 138) if hovered else backdrop)
            TextRenderer.DrawText(
                graphics,
                sender.Text,
                sender.Font,
                Rectangle(10, 0, max(1, sender.Width - 18), sender.Height),
                sender.ForeColor,
                TextFormatFlags.Left
                | TextFormatFlags.VerticalCenter
                | TextFormatFlags.SingleLine
                | TextFormatFlags.NoPadding,
            )
        except Exception:
            pass

    def on_enter(_sender, _event):
        item._hud_hover = True
        item.Invalidate()

    def on_leave(_sender, _event):
        item._hud_hover = False
        item.Invalidate()

    item.Paint += on_paint
    item.MouseEnter += on_enter
    item.MouseLeave += on_leave


def _paint_menu_separator(separator, backdrop) -> None:
    """Draw a slim indigo separator on the dark menu surface."""
    from System.Drawing import Color, Pen, Rectangle
    from System.Drawing.Drawing2D import SmoothingMode

    def on_paint(sender, event):
        try:
            graphics = event.Graphics
            graphics.SmoothingMode = SmoothingMode.AntiAlias
            graphics.Clear(backdrop)
            pen = Pen(Color.FromArgb(79, 70, 229), 1.0)
            try:
                y = max(1, sender.Height // 2)
                graphics.DrawLine(pen, 6, y, max(7, sender.Width - 6), y)
            finally:
                pen.Dispose()
        except Exception:
            pass

    separator.Paint += on_paint


def _apply_dark_menu_theme(menu) -> None:
    """Dark high-contrast styling for a ``ContextMenuStrip`` (0 tofu boxes)."""
    from System.Drawing import Color, Font
    from System.Windows.Forms import ToolStripRenderMode

    menu.BackColor = Color.FromArgb(15, 23, 42)
    menu.ForeColor = Color.FromArgb(241, 245, 249)
    menu.ShowImageMargin = False
    menu.Font = Font("Microsoft YaHei UI", 9.25)
    try:
        menu.Renderer = _make_dark_menu_renderer()
        menu.RenderMode = ToolStripRenderMode.Professional
    except Exception:
        try:
            menu.RenderMode = ToolStripRenderMode.System
        except Exception:
            pass


class HudQuickCredentialForm:
    """Floating dark Quick-Credential card anchored beneath the PurpleShield Pilot HUD."""

    def __init__(
        self,
        account: dict,
        anchor_point: tuple[int, int],
        get_totp_offset: Callable[[], int],
        paste_value_fn: Callable[[str], None],
        save_credentials_fn: Callable[[int, dict], dict],
        get_top_recovery_fn: Callable[[], list[dict]] | None = None,
        on_saved_fn: Callable[[dict, str], None] | None = None,
        on_closed_fn: Callable[[], None] | None = None,
    ):
        from System.Drawing import Color, ContentAlignment, Font, FontStyle, Point, Size, SizeF
        from System.Windows.Forms import (
            AutoScaleMode,
            BorderStyle,
            Button,
            CheckBox,
            Clipboard,
            ColumnStyle,
            Cursors,
            DockStyle,
            FlatStyle,
            FlowDirection,
            FlowLayoutPanel,
            Form,
            FormBorderStyle,
            FormStartPosition,
            Keys,
            Label,
            Padding,
            Panel,
            RowStyle,
            Screen,
            SizeType,
            TableLayoutPanel,
            TextBox,
            TextRenderer,
            Timer,
        )

        self._account = dict(account or {})
        self._get_totp_offset = get_totp_offset
        self._paste_value_fn = paste_value_fn
        self._save_credentials_fn = save_credentials_fn
        self._get_top_recovery_fn = get_top_recovery_fn
        self._on_saved_fn = on_saved_fn
        self._on_closed_fn = on_closed_fn
        self._current_preview_code = ""
        self._is_google = (self._account.get("account_type") or "").strip().lower() == "google"
        self._saving = False
        self._btn_save = None

        bg_main = Color.FromArgb(15, 23, 42)
        bg_card = Color.FromArgb(2, 6, 23)
        bg_btn = Color.FromArgb(30, 41, 59)
        border_indigo = Color.FromArgb(99, 102, 241)
        border_slate = Color.FromArgb(30, 41, 59)
        fg_main = Color.FromArgb(226, 232, 240)
        fg_muted = Color.FromArgb(148, 163, 184)

        form = Form()
        form.Text = "极速改密 / 换绑 2FA"
        form.FormBorderStyle = getattr(FormBorderStyle, "None")
        form.StartPosition = FormStartPosition.Manual
        form.TopMost = True
        form.ShowInTaskbar = False
        form.KeyPreview = True
        form.AutoScaleDimensions = SizeF(96.0, 96.0)
        form.AutoScaleMode = getattr(AutoScaleMode, "None")
        form.Font = Font("Microsoft YaHei UI", 9.0)
        form.BackColor = bg_main
        form.ForeColor = fg_main
        self.form = form

        def on_key_down(_s, ke):
            if ke.KeyCode == Keys.Escape:
                ke.Handled = True
                form.Close()

        form.KeyDown += on_key_down

        root = Panel()
        root.Dock = DockStyle.Fill
        root.BackColor = bg_main
        root.Padding = Padding(14, 10, 14, 12)
        _bind_rounded_shape(form, 12, None)
        _bind_rounded_shape(root, 12, border_indigo)

        layout = TableLayoutPanel()
        layout.Dock = DockStyle.Top
        layout.ColumnCount = 1
        layout.RowCount = 6
        layout.AutoSize = True
        layout.BackColor = Color.Transparent
        layout.Margin = Padding(0)
        layout.Padding = Padding(0)
        layout.ColumnStyles.Add(ColumnStyle(SizeType.Percent, 100.0))

        def make_btn(text: str, bg, fg, min_w: int = 0, height: int = 24, bold: bool = False):
            f = Font("Microsoft YaHei UI", 9.0, FontStyle.Bold if bold else FontStyle.Regular)
            label = _sanitize_gdi_text(text)
            measured = TextRenderer.MeasureText(label or " ", f)
            btn = Button()
            btn.Text = label
            btn.AutoSize = False
            btn.Width = max(min_w, measured.Width + 16)
            btn.Height = height
            btn.FlatStyle = FlatStyle.Flat
            btn.BackColor = bg
            btn.ForeColor = fg
            btn.Cursor = Cursors.Hand
            btn.Font = f
            btn.Margin = Padding(0, 0, 4, 2)
            btn.TextAlign = ContentAlignment.MiddleCenter
            btn.FlatAppearance.BorderSize = 1
            btn.FlatAppearance.BorderColor = Color.FromArgb(51, 65, 85)
            return btn

        def make_card():
            card = TableLayoutPanel()
            card.Dock = DockStyle.Top
            card.ColumnCount = 1
            card.AutoSize = True
            card.BackColor = bg_card
            card.Padding = Padding(10, 7, 10, 8)
            card.Margin = Padding(0, 0, 0, 7)
            card.ColumnStyles.Add(ColumnStyle(SizeType.Percent, 100.0))
            _bind_rounded_shape(card, 8, border_slate)
            return card

        def make_header_flow(title: str):
            flow = FlowLayoutPanel()
            flow.Dock = DockStyle.Top
            flow.FlowDirection = FlowDirection.LeftToRight
            flow.WrapContents = True
            flow.AutoSize = True
            flow.BackColor = Color.Transparent
            flow.Margin = Padding(0, 0, 0, 4)
            lbl = Label()
            lbl.AutoSize = True
            lbl.Text = _sanitize_gdi_text(title)
            lbl.Font = Font("Microsoft YaHei UI", 9.0, FontStyle.Bold)
            lbl.ForeColor = fg_main
            lbl.Margin = Padding(0, 3, 8, 2)
            flow.Controls.Add(lbl)
            return flow

        def make_textbox(initial_text: str, fg_color):
            tb = TextBox()
            tb.Dock = DockStyle.Top
            tb.Text = initial_text
            tb.BorderStyle = BorderStyle.FixedSingle
            tb.BackColor = bg_main
            tb.ForeColor = fg_color
            tb.Font = Font("Consolas", 9.75)
            return tb

        # 1. Header (two clean stacked rows so long emails stay flush-left and never collide with ×)
        header = Panel()
        header.Dock = DockStyle.Top
        header.Height = 48
        header.Margin = Padding(0, 0, 0, 6)
        header.BackColor = Color.Transparent
        header.Cursor = Cursors.SizeAll
        header.MouseDown += lambda _s, me: _win32_drag_window(self.handle) if str(me.Button) == "Left" else None

        header_row1 = Panel()
        header_row1.Dock = DockStyle.Top
        header_row1.Height = 26
        header_row1.BackColor = Color.Transparent
        header_row1.Cursor = Cursors.SizeAll
        header_row1.MouseDown += lambda _s, me: _win32_drag_window(self.handle) if str(me.Button) == "Left" else None

        lbl_title = Label()
        lbl_title.AutoSize = True
        lbl_title.Location = Point(0, 2)
        lbl_title.Text = _sanitize_gdi_text("⚡ 极速改密 / 换绑 2FA")
        lbl_title.Font = Font("Microsoft YaHei UI", 9.25, FontStyle.Bold)
        lbl_title.ForeColor = Color.FromArgb(224, 231, 255)
        lbl_title.Cursor = Cursors.SizeAll
        lbl_title.MouseDown += lambda _s, me: _win32_drag_window(self.handle) if str(me.Button) == "Left" else None

        sec_badge = Label()
        sec_badge.AutoSize = True
        sec_badge.Location = Point(156, 3)
        sec_badge.Font = Font("Microsoft YaHei UI", 8.0, FontStyle.Bold)
        sec_badge.Padding = Padding(5, 1, 5, 1)
        self._sec_badge = sec_badge

        btn_x = make_btn("×", Color.FromArgb(69, 10, 10), Color.FromArgb(252, 165, 165), min_w=26, height=24)
        btn_x.Dock = DockStyle.Right
        btn_x.Click += lambda _s, _e: form.Close()

        header_row1.Controls.Add(lbl_title)
        header_row1.Controls.Add(sec_badge)
        header_row1.Controls.Add(btn_x)

        lbl_email = Label()
        lbl_email.Dock = DockStyle.Fill
        lbl_email.TextAlign = ContentAlignment.MiddleLeft
        lbl_email.Text = _sanitize_gdi_text(str(self._account.get("account") or ""))
        lbl_email.Font = Font("Microsoft YaHei UI", 8.5)
        lbl_email.ForeColor = fg_muted
        lbl_email.AutoEllipsis = True
        lbl_email.Cursor = Cursors.SizeAll
        lbl_email.MouseDown += lambda _s, me: _win32_drag_window(self.handle) if str(me.Button) == "Left" else None
        self._lbl_email = lbl_email

        header.Controls.Add(lbl_email)
        header.Controls.Add(header_row1)

        # 2. Password Card
        pwd_card = make_card()
        pwd_top = make_header_flow("🔑 新登录密码")
        txt_pwd = make_textbox(str(self._account.get("password") or ""), Color.FromArgb(248, 250, 252))
        self._txt_pwd = txt_pwd

        btn_gen_pwd = make_btn("🎲 生成强密", Color.FromArgb(49, 46, 129), Color.FromArgb(224, 231, 255))
        btn_fill_pwd = make_btn("📋 填入网页", bg_btn, Color.FromArgb(165, 243, 252))

        def on_gen_pwd(_s, _e):
            pwd = generate_strong_password(16)
            txt_pwd.Text = pwd
            try:
                Clipboard.SetDataObject(pwd, True, 4, 50)
            except Exception:
                pass
            self._set_feedback("✓ 已生成 16 位强密并复制，点「填入网页」直接粘贴", warning=False)

        def on_fill_pwd(_s, _e):
            val = txt_pwd.Text.strip()
            if not val:
                self._set_feedback("⚠ 密码不能为空", warning=True)
                return
            self._paste_value_fn(val)
            self._set_feedback("✓ 已将密码硬件贴入浏览器当前焦点框", warning=False)

        btn_gen_pwd.Click += on_gen_pwd
        btn_fill_pwd.Click += on_fill_pwd
        pwd_top.Controls.Add(btn_gen_pwd)
        pwd_top.Controls.Add(btn_fill_pwd)
        pwd_card.Controls.Add(pwd_top, 0, 0)
        pwd_card.Controls.Add(txt_pwd, 0, 1)

        # 3. 2FA Card
        twofa_card = make_card()
        twofa_top = make_header_flow("🛡️ 新 2FA 密钥")
        txt_2fa = make_textbox(str(self._account.get("secret_key") or ""), Color.FromArgb(110, 231, 183))
        self._txt_2fa = txt_2fa

        lbl_live_totp = Label()
        lbl_live_totp.Dock = DockStyle.Top
        lbl_live_totp.Height = 22
        lbl_live_totp.TextAlign = ContentAlignment.MiddleLeft
        lbl_live_totp.Font = Font("Consolas", 9.75, FontStyle.Bold)
        lbl_live_totp.Margin = Padding(0, 4, 0, 0)
        self._lbl_live_totp = lbl_live_totp

        btn_paste_2fa = make_btn("📥 粘贴密钥", bg_btn, fg_main)
        btn_fill_2fa = make_btn("📋 填验证码", Color.FromArgb(6, 78, 59), Color.FromArgb(110, 231, 183))

        def on_paste_2fa(_s, _e):
            try:
                if Clipboard.ContainsText():
                    raw = Clipboard.GetText() or ""
                    cleaned = normalize_secret(raw)
                    txt_2fa.Text = cleaned or raw.strip()
                    self._refresh_totp_preview()
                    self._set_feedback("✓ 已从剪贴板解析 2FA 密钥", warning=False)
            except Exception:
                self._set_feedback("⚠ 读取剪贴板失败", warning=True)

        def on_fill_2fa(_s, _e):
            self._refresh_totp_preview()
            if not self._current_preview_code:
                self._set_feedback("⚠ 请先填入有效的 Base32 2FA 密钥", warning=True)
                return
            self._paste_value_fn(self._current_preview_code)
            raw_now = (txt_2fa.Text or "").strip()
            cleaned_now = normalize_secret(raw_now)
            old_2fa = normalize_secret(self._account.get("secret_key") or "")
            if cleaned_now and cleaned_now != old_2fa and (
                not self._is_google or self._chk_google_confirmed.Checked
            ):
                self._save_all()
                return
            self._set_feedback(f"✓ 已填入当前 2FA 验证码 ({self._current_preview_code})", warning=False)

        btn_paste_2fa.Click += on_paste_2fa
        btn_fill_2fa.Click += on_fill_2fa
        txt_2fa.TextChanged += lambda _s, _e: self._refresh_totp_preview()
        twofa_top.Controls.Add(btn_paste_2fa)
        twofa_top.Controls.Add(btn_fill_2fa)
        twofa_card.Controls.Add(twofa_top, 0, 0)
        twofa_card.Controls.Add(txt_2fa, 0, 1)
        twofa_card.Controls.Add(lbl_live_totp, 0, 2)

        # 4. Recovery Email Card
        rec_card = make_card()
        rec_top = make_header_flow("📮 辅助恢复邮箱")
        txt_rec = make_textbox(str(self._account.get("recovery_email") or ""), Color.FromArgb(226, 232, 240))
        self._txt_rec = txt_rec

        btn_paste_rec = make_btn("📥 粘贴", bg_btn, fg_main)
        btn_fill_rec = make_btn("📋 填入网页", bg_btn, Color.FromArgb(165, 243, 252))

        def on_paste_rec(_s, _e):
            try:
                if Clipboard.ContainsText():
                    txt_rec.Text = (Clipboard.GetText() or "").strip()
            except Exception:
                pass

        def on_fill_rec(_s, _e):
            val = txt_rec.Text.strip()
            if not val:
                self._set_feedback("⚠ 辅助邮箱为空", warning=True)
                return
            self._paste_value_fn(val)
            self._set_feedback("✓ 已将辅助邮箱填入浏览器输入框", warning=False)

        btn_paste_rec.Click += on_paste_rec
        btn_fill_rec.Click += on_fill_rec
        rec_top.Controls.Add(btn_paste_rec)
        rec_top.Controls.Add(btn_fill_rec)
        rec_card.Controls.Add(rec_top, 0, 0)
        rec_card.Controls.Add(txt_rec, 0, 1)

        if callable(self._get_top_recovery_fn):
            try:
                chips = self._get_top_recovery_fn() or []
            except Exception:
                chips = []
            if chips:
                chips_flow = FlowLayoutPanel()
                chips_flow.Dock = DockStyle.Top
                chips_flow.FlowDirection = FlowDirection.LeftToRight
                chips_flow.WrapContents = True
                chips_flow.AutoSize = True
                chips_flow.BackColor = Color.Transparent
                chips_flow.Margin = Padding(0, 4, 0, 0)
                for item in chips[:4]:
                    email_val = str(item.get("email") or "").strip()
                    if not email_val:
                        continue
                    chip = make_btn(email_val, Color.FromArgb(15, 23, 42), Color.FromArgb(165, 243, 252), height=22)
                    chip.Click += (lambda val: lambda _s, _e: setattr(txt_rec, "Text", val))(email_val)
                    chips_flow.Controls.Add(chip)
                rec_card.RowCount = 3
                rec_card.Controls.Add(chips_flow, 0, 2)

        # 5. Checkboxes
        chk_flow = FlowLayoutPanel()
        chk_flow.Dock = DockStyle.Top
        chk_flow.FlowDirection = FlowDirection.LeftToRight
        chk_flow.WrapContents = True
        chk_flow.AutoSize = True
        chk_flow.Margin = Padding(0, 2, 0, 6)
        chk_flow.BackColor = Color.Transparent

        sec_info = self._account.get("security") or {}
        chk_devices = CheckBox()
        chk_devices.AutoSize = True
        chk_devices.Text = "已清退陌生设备"
        chk_devices.Checked = bool(sec_info.get("devices_cleared") or self._account.get("sec_devices_cleared"))
        chk_devices.ForeColor = Color.FromArgb(203, 213, 225)
        chk_devices.Margin = Padding(2, 2, 12, 2)
        chk_devices.Cursor = Cursors.Hand
        self._chk_devices = chk_devices

        chk_secured = CheckBox()
        chk_secured.AutoSize = True
        chk_secured.Text = "直接标为「✓ 已改安全」"
        chk_secured.Checked = False
        chk_secured.ForeColor = Color.FromArgb(110, 231, 183)
        chk_secured.Margin = Padding(0, 2, 12, 2)
        chk_secured.Cursor = Cursors.Hand
        self._chk_secured = chk_secured

        chk_flow.Controls.Add(chk_devices)
        chk_flow.Controls.Add(chk_secured)

        chk_google_confirmed = CheckBox()
        chk_google_confirmed.AutoSize = True
        chk_google_confirmed.Text = "我确认已在 Google 网页生效"
        chk_google_confirmed.Checked = False
        chk_google_confirmed.Visible = self._is_google
        chk_google_confirmed.ForeColor = Color.FromArgb(253, 224, 71)
        chk_google_confirmed.Margin = Padding(0, 2, 0, 2)
        chk_google_confirmed.Cursor = Cursors.Hand
        self._chk_google_confirmed = chk_google_confirmed
        if self._is_google:
            chk_flow.Controls.Add(chk_google_confirmed)

        # 6. Footer
        footer = Panel()
        footer.Dock = DockStyle.Top
        footer.Height = 32
        footer.Margin = Padding(0)
        footer.BackColor = Color.Transparent

        lbl_feedback = Label()
        lbl_feedback.Dock = DockStyle.Fill
        lbl_feedback.TextAlign = ContentAlignment.MiddleLeft
        lbl_feedback.Text = "旧密码 / 旧 2FA 将自动归档保留"
        lbl_feedback.Font = Font("Microsoft YaHei UI", 9.0)
        lbl_feedback.ForeColor = fg_muted
        lbl_feedback.AutoEllipsis = True
        self._lbl_feedback = lbl_feedback

        btn_cancel = make_btn("关闭", bg_btn, Color.FromArgb(203, 213, 225), min_w=56, height=28)
        btn_cancel.Dock = DockStyle.Right
        btn_cancel.Click += lambda _s, _e: form.Close()

        btn_spacer = Panel()
        btn_spacer.Dock = DockStyle.Right
        btn_spacer.Width = 8
        btn_spacer.BackColor = Color.Transparent

        btn_save = make_btn("✓ 保存修改", Color.FromArgb(79, 70, 229), Color.White, min_w=104, height=28, bold=True)
        btn_save.Dock = DockStyle.Right
        btn_save.Click += lambda _s, _e: self._save_all()
        self._btn_save = btn_save

        footer.Controls.Add(lbl_feedback)
        footer.Controls.Add(btn_save)
        footer.Controls.Add(btn_spacer)
        footer.Controls.Add(btn_cancel)

        layout.Controls.Add(header, 0, 0)
        layout.Controls.Add(pwd_card, 0, 1)
        layout.Controls.Add(twofa_card, 0, 2)
        layout.Controls.Add(rec_card, 0, 3)
        layout.Controls.Add(chk_flow, 0, 4)
        layout.Controls.Add(footer, 0, 5)

        root.Controls.Add(layout)
        form.Controls.Add(root)

        self._refresh_security_badge()
        self._refresh_totp_preview()

        target_w = round(520 * max(1.0, float(form.DeviceDpi or 96) / 96.0))
        def _sync_bounds(_s=None, _e=None):
            if form.IsDisposed:
                return
            pref_size = layout.GetPreferredSize(Size(max(420, target_w - 28), 0))
            pref_h = max(380, int(pref_size.Height) + 28)
            form.ClientSize = Size(target_w, pref_h)
            self._position_below_anchor(anchor_point, Screen)

        _sync_bounds()
        form.Shown += _sync_bounds

        live_timer = Timer()
        live_timer.Interval = 1000
        live_timer.Tick += lambda _s, _e: self._refresh_totp_preview()
        live_timer.Start()
        self._live_timer = live_timer

        def on_closed(_s, _e):
            try:
                live_timer.Stop()
                live_timer.Dispose()
            except Exception:
                pass
            if self._on_closed_fn:
                try:
                    self._on_closed_fn()
                except Exception:
                    pass

        form.FormClosed += on_closed
        self.handle = int(form.Handle.ToInt64())

    def _position_below_anchor(self, anchor_point: tuple[int, int], screen_cls) -> None:
        from System.Drawing import Point

        ax, ay = anchor_point
        try:
            work = screen_cls.FromPoint(Point(int(ax), int(ay))).WorkingArea
        except Exception:
            work = screen_cls.PrimaryScreen.WorkingArea
        w, h = self.form.Width, self.form.Height
        x = max(work.Left + 8, min(int(ax) - w // 2, max(work.Left + 8, work.Right - w - 8)))
        y = max(work.Top + 8, min(int(ay) + 6, max(work.Top + 8, work.Bottom - h - 8)))
        self.form.Location = Point(x, y)

    def _set_feedback(self, text: str, *, warning: bool = False) -> None:
        from System.Drawing import Color

        if self.form.IsDisposed:
            return
        self._lbl_feedback.ForeColor = (
            Color.FromArgb(251, 191, 36) if warning else Color.FromArgb(52, 211, 153)
        )
        self._lbl_feedback.Text = _sanitize_gdi_text(text)

    def _refresh_security_badge(self) -> None:
        from System.Drawing import Color

        sec_status = (self._account.get("security") or {}).get("status", "unmodified")
        if sec_status == "secured":
            self._sec_badge.Text = "✓ 已改安全"
            self._sec_badge.BackColor = Color.FromArgb(6, 78, 59)
            self._sec_badge.ForeColor = Color.FromArgb(110, 231, 183)
        elif sec_status == "partial":
            self._sec_badge.Text = "◐ 部分加固"
            self._sec_badge.BackColor = Color.FromArgb(120, 53, 15)
            self._sec_badge.ForeColor = Color.FromArgb(253, 224, 71)
        else:
            self._sec_badge.Text = "⚠ 未改安全"
            self._sec_badge.BackColor = Color.FromArgb(127, 29, 29)
            self._sec_badge.ForeColor = Color.FromArgb(254, 202, 202)

    def _refresh_totp_preview(self) -> None:
        from System.Drawing import Color

        if self.form.IsDisposed:
            return
        raw = (self._txt_2fa.Text or "").strip()
        if not raw:
            self._current_preview_code = ""
            self._lbl_live_totp.ForeColor = Color.FromArgb(100, 116, 139)
            self._lbl_live_totp.Text = "实时验证码: 尚未填写 2FA 密钥"
            return
        res = generate_totp(raw, offset_seconds=self._get_totp_offset())
        if not res.get("valid") or not res.get("code"):
            self._current_preview_code = ""
            self._lbl_live_totp.ForeColor = Color.FromArgb(251, 191, 36)
            self._lbl_live_totp.Text = "⚠ 密钥格式无效 (需 Base32 字符)"
            return
        code = res["code"]
        rem = int(res["remaining"])
        self._current_preview_code = code
        fmt = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
        self._lbl_live_totp.ForeColor = (
            Color.FromArgb(253, 224, 71) if rem <= 5 else Color.FromArgb(110, 231, 183)
        )
        self._lbl_live_totp.Text = f"验证码: {fmt}  ({rem:2d}s)"

    def update_account(self, fresh: dict) -> None:
        if not fresh or self.form.IsDisposed:
            return
        prev_secret = normalize_secret(self._account.get("secret_key") or "")
        self._account = dict(fresh)
        fresh_secret = normalize_secret(fresh.get("secret_key") or "")
        if hasattr(self, "_lbl_email"):
            self._lbl_email.Text = _sanitize_gdi_text(str(fresh.get("account") or ""))
        cur_txt = normalize_secret(self._txt_2fa.Text or "")
        if cur_txt == prev_secret and cur_txt != fresh_secret:
            self._txt_2fa.Text = str(fresh.get("secret_key") or "")
        self._refresh_security_badge()
        self._refresh_totp_preview()

    def _save_all(self) -> None:
        if self._saving or self.form.IsDisposed:
            return
        self._saving = True
        if self._btn_save is not None:
            self._btn_save.Enabled = False
        try:
            new_pwd = (self._txt_pwd.Text or "").strip()
            raw_2fa = (self._txt_2fa.Text or "").strip()
            new_2fa = normalize_secret(raw_2fa)
            new_rec = (self._txt_rec.Text or "").strip()
            old_pwd = str(self._account.get("password") or "")
            old_2fa = normalize_secret(self._account.get("secret_key") or "")
            old_rec = str(self._account.get("recovery_email") or "")

            if raw_2fa and (not new_2fa or not generate_totp(new_2fa, at_time=0).get("valid")):
                self._set_feedback("⚠ 2FA 密钥格式无效，请检查 Base32 字符", warning=True)
                return

            pwd_changed = bool(new_pwd != old_pwd)
            twofa_changed = bool(new_2fa != old_2fa)
            rec_changed = bool(new_rec != old_rec)
            force_secured = bool(self._chk_secured.Checked)
            devices_cleared = bool(self._chk_devices.Checked)

            if self._is_google and not self._chk_google_confirmed.Checked:
                if pwd_changed or twofa_changed or rec_changed or force_secured or devices_cleared:
                    self._set_feedback("⚠ Google 账号请先勾选「我确认已在 Google 网页生效」", warning=True)
                    return

            payload: dict = {
                "password": new_pwd,
                "secret_key": new_2fa,
                "recovery_email": new_rec,
                "reason": "hud_quick_edit",
                "sec_devices_cleared": devices_cleared,
            }
            if self._is_google:
                payload["google_confirmed"] = bool(self._chk_google_confirmed.Checked)
            if (new_pwd and new_pwd != old_pwd) or force_secured:
                payload["password_modified"] = 1
            if (new_2fa and new_2fa != old_2fa) or force_secured:
                payload["twofa_modified"] = 1
            if rec_changed or force_secured:
                payload["sec_recovery_modified"] = True
            if force_secured:
                payload["sec_devices_cleared"] = True

            res = self._save_credentials_fn(int(self._account["id"]), payload)
            if not res or not res.get("ok"):
                self._set_feedback(f"⚠ {(res or {}).get('error') or '保存失败'}", warning=True)
                return
            updated = res["account"]
            self._account = updated
            saved_2fa = str(updated.get("secret_key") or "")
            if self._txt_2fa.Text != saved_2fa:
                self._txt_2fa.Text = saved_2fa
            self._refresh_security_badge()
            self._refresh_totp_preview()
            msg = "✓ 凭据已更新并自动归档旧密码/2FA"
            self._set_feedback(msg, warning=False)
            if self._on_saved_fn:
                self._on_saved_fn(updated, msg)
        except Exception as exc:
            log_exception("HudQuickCredentialForm._save_all")
            self._set_feedback(f"⚠ 保存异常: {exc}", warning=True)
        finally:
            self._saving = False
            if not self.form.IsDisposed and self._btn_save is not None:
                self._btn_save.Enabled = True


class LoginToolsStrip:
    """PurpleShield Pilot HUD bar paired with an isolated browser profile."""

    def __init__(
        self,
        label: str,
        copy: Callable[[str], None],
        close: Callable[[], None],
        *,
        account: dict | None = None,
        actions: dict | None = None,
    ):
        from System.Drawing import Color, ContentAlignment, Font, FontStyle, Size, SizeF
        from System.Windows.Forms import (
            AutoScaleMode,
            ContextMenuStrip,
            Cursors,
            DockStyle,
            FlowDirection,
            FlowLayoutPanel,
            Form,
            FormBorderStyle,
            FormStartPosition,
            Label,
            Padding,
            Panel,
            Screen,
            ToolStripSeparator,
            ToolTip,
        )

        self._label = label
        self._account = dict(account or {"account": label})
        self._copy_cb = copy
        self._close_cb = close
        self._actions = dict(actions or {})
        self._message_until = 0.0
        self._is_collapsed = False
        self.user_dragged = False
        self.dragged_pos: tuple[int, int] | None = None
        self._tz_warning_active = False
        self._tz_warning_text = ""
        self._geo_text = "● 原生就绪"
        self._current_2fa_text = "无 2FA"
        self._quick_cred_form: HudQuickCredentialForm | None = None
        self._quick_cred_hidden_by_focus = False
        self._active_menu = None
        self._menu_open = False

        bg_bar = Color.FromArgb(15, 23, 42)
        bg_pill = Color.FromArgb(22, 35, 59)
        bg_mini = Color.FromArgb(17, 28, 51)
        bg_btn = Color.FromArgb(30, 41, 59)
        fg_main = Color.FromArgb(248, 250, 252)
        fg_muted = Color.FromArgb(226, 232, 240)
        border_indigo = Color.FromArgb(79, 70, 229)
        border_mini = Color.FromArgb(129, 140, 248)
        border_btn = Color.FromArgb(71, 85, 105)

        form = Form()
        form.Text = "KeyVault · 紫盾领航栏"
        form.FormBorderStyle = getattr(FormBorderStyle, "None")
        form.StartPosition = FormStartPosition.Manual
        form.ShowInTaskbar = False
        form.ControlBox = False
        form.BackColor = bg_bar
        form.ForeColor = fg_main
        form.Font = Font("Microsoft YaHei UI", 9.0)
        form.AutoScaleDimensions = SizeF(96.0, 96.0)
        form.AutoScaleMode = getattr(AutoScaleMode, "None")
        self.form = form
        dpi_scale = max(1.0, float(getattr(form, "DeviceDpi", 96) or 96) / 96.0)
        self._dpi_scale = dpi_scale
        self._menu_font_reg = Font("Microsoft YaHei UI", 9.25, FontStyle.Regular)
        self._menu_font_bold = Font("Microsoft YaHei UI", 9.25, FontStyle.Bold)

        tooltip = ToolTip()
        tooltip.InitialDelay = 220
        tooltip.ReshowDelay = 120
        self._tooltip = tooltip

        def reset_to_browser_top() -> None:
            """Clear the drag override so the panel re-docks the HUD inside the browser."""
            self.user_dragged = False
            self.dragged_pos = None

        def enable_drag(ctrl, on_double_click=None):
            def on_down(_s, me):
                if str(me.Button) != "Left":
                    return
                now = time.monotonic()
                last = float(getattr(ctrl, "_hud_last_down", 0.0) or 0.0)
                if on_double_click is not None and 0.0 < now - last < 0.45:
                    ctrl._hud_last_down = 0.0
                    on_double_click()
                    return
                ctrl._hud_last_down = now
                pos_before = (int(form.Left), int(form.Top))
                _win32_drag_window(self.handle)
                pos_after = (int(form.Left), int(form.Top))
                if abs(pos_after[0] - pos_before[0]) >= 3 or abs(pos_after[1] - pos_before[1]) >= 3:
                    self.user_dragged = True
                    self.dragged_pos = pos_after

            ctrl.MouseDown += on_down
            if on_double_click is not None:
                ctrl.DoubleClick += lambda _s, _e: on_double_click()

        pill_h = round(30 * dpi_scale)

        def pill(text: str, bg, fg, border, hover, press, *, min_w: int = 0,
                 height: int = pill_h, padding_x: int = 16, bold: bool = False,
                 font_size: float = 9.0, font_name: str = "Microsoft YaHei UI"):
            return _make_pill_button(
                text,
                bg=bg,
                fg=fg,
                border=border,
                hover=hover,
                press=press,
                backdrop=bg_bar,
                min_width=round(min_w * dpi_scale) if min_w else 0,
                height=height,
                padding_x=round(padding_x * dpi_scale),
                bold=bold,
                font_size=font_size,
                font_name=font_name,
            )

        full_panel = Panel()
        full_panel.Dock = DockStyle.Fill
        full_panel.BackColor = bg_bar
        pad_x = round(10 * dpi_scale)
        pad_y = round(7 * dpi_scale)
        full_panel.Padding = Padding(pad_x, pad_y, pad_x, pad_y)
        _bind_rounded_shape(form, 12, None)
        _bind_rounded_shape(full_panel, 12, border_indigo)
        enable_drag(full_panel)
        self._full_panel = full_panel

        main_flow = FlowLayoutPanel()
        main_flow.Dock = DockStyle.Fill
        main_flow.FlowDirection = FlowDirection.LeftToRight
        main_flow.WrapContents = False
        main_flow.AutoSize = False
        main_flow.BackColor = Color.Transparent
        main_flow.Margin = Padding(0)
        main_flow.Padding = Padding(0)
        enable_drag(main_flow)
        self._main_flow = main_flow

        self._badge_base_width = round(144 * dpi_scale)
        badge = Label()
        badge.AutoSize = False
        badge.Width = self._badge_base_width
        badge.Height = pill_h
        badge.TextAlign = ContentAlignment.MiddleLeft
        badge.Padding = Padding(round(8 * dpi_scale), 0, round(6 * dpi_scale), 0)
        badge.BackColor = bg_pill
        badge.ForeColor = Color.FromArgb(224, 231, 255)
        badge.Font = Font("Microsoft YaHei UI", 9.0, FontStyle.Bold)
        badge.Cursor = Cursors.SizeAll
        badge.AutoEllipsis = True
        badge.Margin = Padding(0, 0, round(3 * dpi_scale), 0)
        _bind_rounded_shape(badge, 6, None)
        enable_drag(badge, reset_to_browser_top)
        self._badge_lbl = badge

        btn_email = pill("填邮箱", bg_btn, fg_main, border_btn,
                         Color.FromArgb(51, 65, 85), Color.FromArgb(15, 23, 42), min_w=56, padding_x=12)
        tooltip.SetToolTip(btn_email, "先点击网页邮箱框，再点此按钮：自动填入邮箱并同步复制")
        btn_email.Click += lambda _s, _e: self._on_fill_or_copy("account")

        btn_pwd = pill("填密码", bg_btn, fg_main, border_btn,
                       Color.FromArgb(51, 65, 85), Color.FromArgb(15, 23, 42), min_w=56, padding_x=12)
        tooltip.SetToolTip(btn_pwd, "先点击网页密码框，再点此按钮：自动硬件贴入密码并同步复制")
        btn_pwd.Click += lambda _s, _e: self._on_fill_or_copy("password")
        self._copy_password = btn_pwd

        sample_2fa_f = Font("Consolas", 9.25, FontStyle.Bold)
        try:
            self._btn_2fa_base_width = max(
                round(154 * dpi_scale),
                _measure_text_width("2FA 888 888 (30s)", sample_2fa_f, round(20 * dpi_scale)),
            )
        finally:
            sample_2fa_f.Dispose()

        btn_2fa = pill(
            "无 2FA",
            Color.FromArgb(6, 95, 70),
            Color.FromArgb(209, 250, 229),
            Color.FromArgb(16, 185, 129),
            Color.FromArgb(4, 120, 87),
            Color.FromArgb(6, 78, 59),
            min_w=0,
            padding_x=20,
            bold=True,
            font_size=9.25,
            font_name="Consolas",
        )
        btn_2fa.Width = self._btn_2fa_base_width
        tooltip.SetToolTip(btn_2fa, "点击将当前 6 位 2FA 动态验证码硬件贴入浏览器输入框并复制")
        btn_2fa.Click += lambda _s, _e: self._on_fill_or_copy("totp")
        self._btn_2fa = btn_2fa

        btn_rec = pill("辅邮", bg_btn, fg_muted, border_btn,
                       Color.FromArgb(51, 65, 85), Color.FromArgb(15, 23, 42), min_w=46, padding_x=12)
        tooltip.SetToolTip(btn_rec, "自动填入辅助邮箱并同步复制")
        btn_rec.Click += lambda _s, _e: self._on_fill_or_copy("recovery")

        btn_quick_cred = pill(
            "⚡ 改密/2FA",
            Color.FromArgb(67, 56, 202),
            Color.FromArgb(255, 255, 255),
            Color.FromArgb(129, 140, 248),
            Color.FromArgb(79, 70, 229),
            Color.FromArgb(55, 48, 163),
            padding_x=12,
            bold=True,
        )
        tooltip.SetToolTip(btn_quick_cred, "在浏览器内部顶部直接弹出「极速改密 / 换绑 2FA」浮窗，旧凭据自动归档")
        btn_quick_cred.Click += lambda _s, _e: self._toggle_quick_cred_popup()
        self._btn_quick_cred = btn_quick_cred

        btn_scan = pill(
            "⚡ 扫码验证 ▾",
            Color.FromArgb(15, 41, 66),
            Color.FromArgb(186, 230, 253),
            Color.FromArgb(56, 189, 248),
            Color.FromArgb(20, 58, 92),
            Color.FromArgb(11, 31, 51),
            padding_x=12,
        )
        tooltip.SetToolTip(btn_scan, "左键打开剪贴板授权/Verify链接或菜单；提供 OAuth 扫码、外链接管、清理会话")
        btn_scan.Click += lambda _s, _e: self._on_scan_verify_click(btn_scan, ContextMenuStrip, ToolStripSeparator)

        btn_gov = pill(
            "常用工具 ▾",
            bg_btn,
            Color.FromArgb(226, 232, 240),
            Color.FromArgb(100, 116, 139),
            Color.FromArgb(51, 65, 85),
            Color.FromArgb(15, 23, 42),
            padding_x=12,
        )
        tooltip.SetToolTip(btn_gov, "快速在当前隔离浏览器打开：安全检查、扫码验证、归属地改区、年龄验证、申诉表")
        btn_gov.Click += lambda _s, _e: self._show_links_menu(btn_gov, ContextMenuStrip, ToolStripSeparator)
        self._btn_gov = btn_gov
        self._btn_scan = btn_scan

        for ctrl in (badge, btn_email, btn_pwd, btn_2fa, btn_rec,
                     btn_quick_cred, btn_scan, btn_gov):
            main_flow.Controls.Add(ctrl)

        # Right fixed cluster docked to the right with clean spacing from action buttons.
        right_sep = round(10 * dpi_scale)
        right_panel = Panel()
        right_panel.Dock = DockStyle.Right
        right_panel.BackColor = Color.Transparent
        right_panel.Margin = Padding(0)
        right_panel.Padding = Padding(right_sep, 0, 0, 0)
        right_flow = FlowLayoutPanel()
        right_flow.Dock = DockStyle.Fill
        right_flow.FlowDirection = FlowDirection.RightToLeft
        right_flow.WrapContents = False
        right_flow.BackColor = Color.Transparent
        right_flow.Margin = Padding(0)
        right_flow.Padding = Padding(0)

        status_lbl = Label()
        status_lbl.AutoSize = False
        status_lbl.Width = round(154 * dpi_scale)
        status_lbl.Height = pill_h
        status_lbl.TextAlign = ContentAlignment.MiddleLeft
        status_lbl.Padding = Padding(round(9 * dpi_scale), 0, round(6 * dpi_scale), 0)
        status_lbl.BackColor = bg_pill
        status_lbl.ForeColor = Color.FromArgb(110, 231, 183)
        status_lbl.Font = Font("Microsoft YaHei UI", 8.5, FontStyle.Bold)
        status_lbl.Text = self._geo_text
        status_lbl.AutoEllipsis = True
        status_lbl.Margin = Padding(0)
        status_lbl.Cursor = Cursors.SizeAll
        _bind_rounded_shape(status_lbl, 6, Color.FromArgb(51, 65, 85))
        enable_drag(status_lbl)
        self._status_lbl = status_lbl

        btn_collapse = pill(
            "—",
            bg_btn,
            Color.FromArgb(226, 232, 240),
            border_btn,
            Color.FromArgb(51, 65, 85),
            Color.FromArgb(15, 23, 42),
            min_w=30,
            padding_x=8,
            font_size=9.5,
        )
        btn_collapse.Margin = Padding(round(4 * dpi_scale), 0, 0, 0)
        tooltip.SetToolTip(btn_collapse, "收起为迷你悬浮胶囊")
        btn_collapse.Click += lambda _s, _e: self.set_collapsed(True, Screen)

        btn_close = pill(
            "×",
            Color.FromArgb(127, 29, 29),
            Color.FromArgb(254, 202, 202),
            Color.FromArgb(185, 28, 28),
            Color.FromArgb(185, 28, 28),
            Color.FromArgb(153, 27, 27),
            min_w=30,
            padding_x=8,
            font_size=10.0,
        )
        btn_close.Margin = Padding(round(4 * dpi_scale), 0, 0, 0)
        tooltip.SetToolTip(btn_close, "关闭领航栏（不影响已打开的浏览器，可在 KeyVault 点「贴到浏览器」重开）")
        btn_close.Click += lambda _s, _e: self._close_cb()

        right_flow.Controls.Add(btn_close)
        right_flow.Controls.Add(btn_collapse)
        right_flow.Controls.Add(status_lbl)
        right_panel.Controls.Add(right_flow)
        right_panel.Width = (
            right_sep
            + status_lbl.Width
            + btn_collapse.Width
            + btn_collapse.Margin.Left
            + btn_close.Width
            + btn_close.Margin.Left
            + round(2 * dpi_scale)
        )
        self._right_panel = right_panel

        # Fill first, then the right-docked cluster claims its strip.
        full_panel.Controls.Add(main_flow)
        full_panel.Controls.Add(right_panel)

        self._expanded_width = self._compute_expanded_width()
        self._bar_height = round(44 * dpi_scale)
        self._mini_width = round(240 * dpi_scale)
        self._mini_height = round(36 * dpi_scale)

        # Mini collapsed capsule
        mini_panel = Panel()
        mini_panel.Dock = DockStyle.Fill
        mini_panel.BackColor = bg_mini
        mini_panel.Padding = Padding(round(10 * dpi_scale), round(5 * dpi_scale), round(6 * dpi_scale), round(5 * dpi_scale))
        mini_panel.Visible = False
        _bind_rounded_shape(mini_panel, 12, border_mini)
        enable_drag(mini_panel, lambda: self.set_collapsed(False, Screen))
        self._mini_panel = mini_panel

        mini_expand = pill(
            "展开",
            Color.FromArgb(67, 56, 202),
            Color.FromArgb(224, 231, 255),
            Color.FromArgb(129, 140, 248),
            Color.FromArgb(79, 70, 229),
            Color.FromArgb(55, 48, 163),
            min_w=46,
            height=round(26 * dpi_scale),
            padding_x=12,
            font_size=8.75,
        )
        mini_expand.Dock = DockStyle.Right
        mini_expand.Margin = Padding(round(4 * dpi_scale), 0, 0, 0)
        mini_expand.Click += lambda _s, _e: self.set_collapsed(False, Screen)

        mini_lbl = Label()
        mini_lbl.Dock = DockStyle.Fill
        mini_lbl.Text = _sanitize_gdi_text(f"⠿ {label}")
        mini_lbl.TextAlign = ContentAlignment.MiddleLeft
        mini_lbl.ForeColor = Color.FromArgb(224, 231, 255)
        mini_lbl.Font = Font("Microsoft YaHei UI", 9.0, FontStyle.Bold)
        mini_lbl.Cursor = Cursors.SizeAll
        mini_lbl.AutoEllipsis = True
        mini_lbl.Margin = Padding(0)
        enable_drag(mini_lbl, lambda: self.set_collapsed(False, Screen))
        self._mini_lbl = mini_lbl

        mini_panel.Controls.Add(mini_lbl)
        mini_panel.Controls.Add(mini_expand)

        form.Controls.Add(full_panel)
        form.Controls.Add(mini_panel)
        form.ClientSize = Size(self._expanded_width, self._bar_height)

        def on_form_resize(_s, _e):
            if self._is_collapsed or form.IsDisposed:
                return
            deficit = self._expanded_width - int(form.ClientSize.Width)
            min_badge = round(68 * dpi_scale)
            target_badge = max(min_badge, self._badge_base_width - max(0, deficit))
            if self._badge_lbl.Width != target_badge:
                self._badge_lbl.Width = target_badge

        form.SizeChanged += on_form_resize

        def prepare_handles(ctrl):
            h = int(ctrl.Handle.ToInt64())
            for ch in ctrl.Controls:
                if not ch.IsHandleCreated:
                    prepare_handles(ch)
            return h

        self.handle = prepare_handles(form)
        self.set_account(label, bool(self._account.get("password")), self._account)
        self.set_code("", 0)

    def _compute_expanded_width(self) -> int:
        scale = getattr(self, "_dpi_scale", 1.0)
        pad_x = self._full_panel.Padding.Left + self._full_panel.Padding.Right
        total_w = pad_x + self._right_panel.Width + round(2 * scale)
        for c in self._main_flow.Controls:
            base_w = self._badge_base_width if c is self._badge_lbl else int(c.Width)
            total_w += base_w + int(c.Margin.Left) + int(c.Margin.Right)
        return max(round(860 * scale), total_w)

    @property
    def is_menu_open(self) -> bool:
        return bool(self._menu_open and self._active_menu is not None and not self._active_menu.IsDisposed)

    @property
    def size(self) -> tuple[int, int]:
        if self._is_collapsed:
            return int(self._mini_width), int(self._mini_height)
        return int(self._expanded_width), int(self._bar_height)

    @property
    def quick_cred_handle(self) -> int:
        if self._quick_cred_form and not self._quick_cred_form.form.IsDisposed:
            return int(self._quick_cred_form.handle)
        return 0

    def set_collapsed(self, collapsed: bool, screen_cls=None) -> None:
        from System.Drawing import Size

        if self._is_collapsed == collapsed:
            return
        self._is_collapsed = collapsed
        if collapsed:
            self.hide_popups()
            self._full_panel.Visible = False
            self._mini_panel.Visible = True
            self.form.ClientSize = Size(int(self._mini_width), int(self._mini_height))
        else:
            self._mini_panel.Visible = False
            self._full_panel.Visible = True
            w = int(self._expanded_width)
            h = int(self._bar_height)
            if screen_cls is not None:
                try:
                    work = screen_cls.FromControl(self.form).WorkingArea
                    w = min(w, max(480, work.Width - 16))
                    if self.form.Left + w > work.Right:
                        self.form.Left = max(work.Left + 6, work.Right - w - 6)
                except Exception:
                    pass
            self.form.ClientSize = Size(w, h)
        if self.user_dragged:
            self.dragged_pos = (int(self.form.Left), int(self.form.Top))

    def set_account(self, label: str, has_password: bool, account: dict | None = None) -> None:
        self._label = label
        if account is not None:
            self._account = dict(account)
        self._copy_password.Enabled = bool(has_password)
        clean = _sanitize_gdi_text(label)
        self._badge_lbl.Text = _sanitize_gdi_text(f"⠿ {clean}")
        self._mini_lbl.Text = _sanitize_gdi_text(f"⠿ {self._current_2fa_text}")
        self._refresh_tooltip()
        if self._quick_cred_form and not self._quick_cred_form.form.IsDisposed and account is not None:
            self._quick_cred_form.update_account(self._account)

    def _refresh_tooltip(self) -> None:
        fp = self._account.get("fp_config") or {}
        exit_ip = fp.get("last_exit_ip") or fp.get("proxy_node") or fp.get("proxy") or "直连"
        tz = fp.get("timezone") or "系统默认"
        loc = fp.get("locale") or "en-US"
        acc_id = self._account.get("id") or ""
        tz_line = (
            _sanitize_gdi_text(f"\n⚠ 时区不一致: {self._tz_warning_text}")
            if self._tz_warning_active
            else ""
        )
        tip = _sanitize_gdi_text(
            f"PurpleShield 纯原生隔离环境（按住拖动位置；双击归位；切回主软件自动隐身）\n"
            f"账号: {self._label}\n"
            f"沙箱目录: data/browsers/acc_{acc_id}\n"
            f"出口/节点: {exit_ip} | {tz} ({loc}){tz_line}"
        )
        self._tooltip.SetToolTip(self._badge_lbl, tip)

    def set_code(self, code: str, remaining: int) -> None:
        from System.Drawing import Color, Size

        remaining = int(remaining or 0)
        scale = getattr(self, "_dpi_scale", 1.0)
        base_min = getattr(self, "_btn_2fa_base_width", round(154 * scale))
        pad_x = round(20 * scale)
        old_btn_w = int(self._btn_2fa.Width)
        if code:
            fmt = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
            self._current_2fa_text = f"2FA {fmt} ({remaining}s)"
            self._btn_2fa.Text = _sanitize_gdi_text(self._current_2fa_text)
            _fit_pill_width(self._btn_2fa, min_width=base_min, padding_x=pad_x)
            self._btn_2fa.Enabled = True
            if remaining <= 5:
                _set_pill_colors(
                    self._btn_2fa,
                    bg=Color.FromArgb(120, 53, 15),
                    hover=Color.FromArgb(146, 64, 14),
                    press=Color.FromArgb(105, 45, 10),
                    border=Color.FromArgb(245, 158, 11),
                    fg=Color.FromArgb(254, 240, 138),
                )
            else:
                _set_pill_colors(
                    self._btn_2fa,
                    bg=Color.FromArgb(6, 95, 70),
                    hover=Color.FromArgb(4, 120, 87),
                    press=Color.FromArgb(6, 78, 59),
                    border=Color.FromArgb(16, 185, 129),
                    fg=Color.FromArgb(209, 250, 229),
                )
        else:
            self._current_2fa_text = "无 2FA"
            self._btn_2fa.Text = "无 2FA"
            _fit_pill_width(self._btn_2fa, min_width=base_min, padding_x=pad_x)
            self._btn_2fa.Enabled = True
            _set_pill_colors(
                self._btn_2fa,
                bg=Color.FromArgb(30, 41, 59),
                hover=Color.FromArgb(51, 65, 85),
                press=Color.FromArgb(15, 23, 42),
                border=Color.FromArgb(71, 85, 105),
                fg=Color.FromArgb(148, 163, 184),
            )

        if int(self._btn_2fa.Width) != old_btn_w and hasattr(self, "_compute_expanded_width"):
            self._expanded_width = self._compute_expanded_width()
            if not self._is_collapsed and not self.form.IsDisposed:
                self.form.ClientSize = Size(int(self._expanded_width), int(self._bar_height))

        self._mini_lbl.Text = _sanitize_gdi_text(f"⠿ {self._current_2fa_text}")

        if self._message_until and time.monotonic() >= self._message_until:
            self._message_until = 0.0
            self._restore_idle_status()

    def _restore_idle_status(self) -> None:
        from System.Drawing import Color

        if self._tz_warning_active:
            self._status_lbl.ForeColor = Color.FromArgb(252, 211, 77)
            self._status_lbl.Text = _sanitize_gdi_text(self._tz_warning_text)
        else:
            self._status_lbl.ForeColor = Color.FromArgb(110, 231, 183)
            self._status_lbl.Text = _sanitize_gdi_text(self._geo_text)

    def set_message(self, message: str, *, is_warning: bool = False) -> None:
        from System.Drawing import Color

        text = _sanitize_gdi_text(message)
        self._message_until = time.monotonic() + 2.8
        self._status_lbl.Text = text
        if is_warning or text.startswith("⚠"):
            self._status_lbl.ForeColor = Color.FromArgb(252, 211, 77)
        else:
            self._status_lbl.ForeColor = Color.FromArgb(110, 231, 183)

    def set_geo_status(self, text: str, *, tooltip_text: str = "", is_warning: bool = False) -> None:
        from System.Drawing import Color

        clean = _sanitize_gdi_text(text)
        if is_warning:
            self._tz_warning_active = True
            self._tz_warning_text = clean
        else:
            self._geo_text = clean
        if tooltip_text:
            self._tooltip.SetToolTip(self._status_lbl, _sanitize_gdi_text(tooltip_text))
        self._refresh_tooltip()
        if self._message_until == 0.0:
            self._restore_idle_status()
            if is_warning:
                self._status_lbl.ForeColor = Color.FromArgb(252, 211, 77)

    def ensure_visible_topmost(self, bounds: tuple[int, int, int, int] | None = None) -> None:
        """Ensure the WinForms strip is visible and carries Win32 ``WS_EX_TOPMOST``.

        A WinForms ``Form`` whose ``Visible`` stays ``False`` (even when revealed
        via ``SetWindowPos(..., SWP_SHOWWINDOW)``) can drop ``HWND_TOPMOST`` and
        sink behind the Chromium window it docks inside.
        """
        form = getattr(self, "form", None)
        if form is None or getattr(form, "IsDisposed", False):
            return
        if bounds is not None and not form.Visible:
            x, y, w, h = (int(v) for v in bounds)
            if w > 0 and h > 0:
                form.SetBounds(x, y, w, h)
        if not form.Visible:
            form.Visible = True
        if not form.TopMost:
            form.TopMost = True
        from src.browser_window import is_window_topmost

        if not is_window_topmost(self.handle):
            form.TopMost = False
            form.TopMost = True

    def hide_popups(self) -> None:
        if self._quick_cred_form and not self._quick_cred_form.form.IsDisposed:
            if self._quick_cred_form.form.Visible:
                self._quick_cred_hidden_by_focus = True
                self._quick_cred_form.form.Visible = False
        if self._active_menu is not None and not self._active_menu.IsDisposed and self._menu_open:
            try:
                self._active_menu.Close()
            except Exception:
                pass

    def restore_popups(self) -> None:
        if (
            getattr(self, "_quick_cred_hidden_by_focus", False)
            and self._quick_cred_form
            and not self._quick_cred_form.form.IsDisposed
        ):
            self._quick_cred_hidden_by_focus = False
            self._quick_cred_form.form.Visible = True
            self._quick_cred_form.form.TopMost = False
            self._quick_cred_form.form.TopMost = True

    def dispose(self) -> None:
        if getattr(self, "_active_menu", None) is not None and not self._active_menu.IsDisposed:
            try:
                self._active_menu.Close()
                self._active_menu.Dispose()
            except Exception:
                pass
            self._active_menu = None
            self._menu_open = False
        if self._quick_cred_form and not self._quick_cred_form.form.IsDisposed:
            try:
                self._quick_cred_form.form.Close()
                self._quick_cred_form.form.Dispose()
            except Exception:
                pass
            self._quick_cred_form = None
        for obj in (
            getattr(self, "_menu_font_reg", None),
            getattr(self, "_menu_font_bold", None),
            self._tooltip,
            self.form,
        ):
            if obj is None:
                continue
            try:
                obj.Dispose()
            except Exception:
                pass

    def _on_fill_or_copy(self, field: str) -> None:
        fill_fn = self._actions.get("smart_fill")
        if callable(fill_fn):
            fill_fn(field)
        else:
            self._copy_cb(field)

    def _toggle_quick_cred_popup(self) -> None:
        if self._quick_cred_form and not self._quick_cred_form.form.IsDisposed:
            self._quick_cred_form.form.Close()
            self._quick_cred_form = None
            return
        save_fn = self._actions.get("quick_update")
        paste_fn = self._actions.get("paste_value")
        if not callable(save_fn) or not callable(paste_fn):
            self.set_message("请在主窗口「养护」中修改凭据", is_warning=True)
            return
        from System.Drawing import Point

        pt = self._btn_quick_cred.PointToScreen(
            Point(self._btn_quick_cred.Width // 2, self._btn_quick_cred.Height)
        )
        anchor = (int(pt.X), int(self.form.Bottom))

        def on_saved(updated: dict, msg: str):
            self.set_account(updated.get("account") or self._label, bool(updated.get("password")), updated)
            self.set_message(msg)

        self._quick_cred_form = HudQuickCredentialForm(
            self._account,
            anchor,
            self._actions.get("get_totp_offset", lambda: 0),
            paste_fn,
            save_fn,
            get_top_recovery_fn=self._actions.get("get_top_recovery"),
            on_saved_fn=on_saved,
            on_closed_fn=lambda: setattr(self, "_quick_cred_form", None),
        )
        self._quick_cred_form.form.Show()
        self._quick_cred_form.form.TopMost = False
        self._quick_cred_form.form.TopMost = True
        self._quick_cred_form.form.Activate()

    def _on_scan_verify_click(self, anchor_btn, menu_cls, sep_cls) -> None:
        from System.Windows.Forms import Clipboard

        clip = ""
        try:
            if Clipboard.ContainsText():
                clip = (Clipboard.GetText() or "").strip()
        except Exception:
            clip = ""
        if clip.lower().startswith(("http://", "https://")) and any(
            k in clip.lower()
            for k in (
                "accounts.google.com",
                "myaccount.google.com",
                "continue?",
                "flowname=",
                "oauth",
                "claude.ai",
                "chatgpt.com",
                "openai.com",
            )
        ):
            open_url_fn = self._actions.get("open_url")
            if callable(open_url_fn):
                open_url_fn(clip)
                self.set_message("✓ 已加载剪贴板验证/授权页")
                return
        self._show_links_menu(anchor_btn, menu_cls, sep_cls)

    def _show_links_menu(self, anchor_btn, menu_cls, sep_cls) -> None:
        from System.Drawing import Color, Point

        if getattr(self, "_active_menu", None) is not None:
            prev = self._active_menu
            self._active_menu = None
            self._menu_open = False
            try:
                if not prev.IsDisposed:
                    prev.Close()
                    prev.Dispose()
            except Exception:
                pass

        menu = menu_cls()
        self._active_menu = menu

        def _on_opened(_s, _e):
            self._menu_open = True

        def _on_closed(_s, _e):
            if self._active_menu is menu:
                self._menu_open = False

        try:
            menu.Opened += _on_opened
            menu.Closed += _on_closed
        except Exception:
            pass
        _apply_dark_menu_theme(menu)

        item_color = Color.FromArgb(241, 245, 249)
        backdrop = Color.FromArgb(15, 23, 42)
        font_reg = getattr(self, "_menu_font_reg", None)
        font_bold = getattr(self, "_menu_font_bold", None)

        def add_action(text: str, handler: Callable[[], None], *, color=None, bold: bool = False):
            item = menu.Items.Add(_sanitize_gdi_text(text))
            item.ForeColor = color or item_color
            item.BackColor = backdrop
            if bold and font_bold is not None:
                item.Font = font_bold
            elif font_reg is not None:
                item.Font = font_reg
            _paint_menu_item(item, backdrop)
            item.Click += lambda _s, _e: handler()
            return item

        def add_separator():
            separator = sep_cls()
            separator.BackColor = backdrop
            _paint_menu_separator(separator, backdrop)
            menu.Items.Add(separator)
            return separator

        def add_url(text: str, url: str):
            def _open():
                fn = self._actions.get("open_url")
                if callable(fn):
                    fn(url)
                    self.set_message(f"已打开: {_sanitize_gdi_text(text).split(' ', 1)[-1]}")

            return add_action(text, _open)

        def add_bind_2fa():
            if callable(self._actions.get("bind_2fa_clipboard")):
                add_action(
                    "◆ 从剪贴板绑定/录入 2FA 密钥",
                    self._actions["bind_2fa_clipboard"],
                    color=Color.FromArgb(251, 191, 36),
                    bold=True,
                )

        platform = (self._account.get("account_type") or "").strip().lower()
        if platform != "google":
            add_bind_2fa()
            if platform == "域名邮箱":
                add_url("◆ 打开邮箱收件箱", "https://mail.ggb6.com/")
            add_url("★ ChatGPT 对话主页", "https://chatgpt.com/")
            add_url("✦ Claude 对话主页", "https://claude.ai/")
            menu.Show(anchor_btn, Point(0, anchor_btn.Height + 2))
            return

        if callable(self._actions.get("start_oauth")):
            add_action(
                "▸ 反重力首次登录/OAuth 扫码授权",
                self._actions["start_oauth"],
                color=Color.FromArgb(52, 211, 153),
                bold=True,
            )

        is_intercept_fn = self._actions.get("is_url_intercept_enabled")
        toggle_intercept_fn = self._actions.get("toggle_url_intercept")
        if callable(is_intercept_fn) and callable(toggle_intercept_fn):
            enabled = bool(is_intercept_fn())
            add_action(
                "⚡ 外链智能接管 (Google/Claude/GPT): [已接管 ✓ 点击关闭]"
                if enabled
                else "⚡ 外链智能接管 (Google/Claude/GPT): [未开启 ⚠ 点击开启]",
                toggle_intercept_fn,
                color=Color.FromArgb(129, 140, 248) if enabled else Color.FromArgb(251, 191, 36),
                bold=True,
            )

        if callable(self._actions.get("inject_rt")):
            add_action(
                "↻ 一键注入 RT → 切换反重力账号 (免Verify免风控)",
                self._actions["inject_rt"],
                color=Color.FromArgb(96, 165, 250),
                bold=True,
            )

        add_separator()

        add_url("▸ 打开 Google 账号注册页面 (养号专用)", "https://accounts.google.com/signup")
        if callable(self._actions.get("copy_persona")):
            add_action(
                "✓ 复制当前账号注册资料 (账号/密码/2FA/辅邮/地区)",
                self._actions["copy_persona"],
                color=Color.FromArgb(167, 139, 250),
            )
        add_bind_2fa()

        add_separator()

        if callable(self._actions.get("probe_verify")):
            add_action(
                "◆ 自动拉取并打开本号实时 Google 验证页",
                self._actions["probe_verify"],
                color=Color.FromArgb(110, 231, 183),
                bold=True,
            )
        if callable(self._actions.get("reset_session")):
            add_action(
                "↻ 清理异常登录/被拒会话并重载登录页",
                self._actions["reset_session"],
                color=Color.FromArgb(251, 146, 60),
                bold=True,
            )
        if callable(self._actions.get("clear_ag_cache")):
            add_action(
                "↻ 一键清除反重力本地 Session 缓存",
                self._actions["clear_ag_cache"],
                color=Color.FromArgb(252, 211, 77),
            )

        add_separator()
        add_url("◆ Google 安全检查 (解除风控与异常提示)", "https://myaccount.google.com/security-checkup")
        add_url("◆ 已授权第三方应用 (移除旧反重力授权)", "https://myaccount.google.com/permissions")
        add_url("◆ 账号找回与恢复 (g.co/recover)", "https://g.co/recover")
        add_url("▸ 审批提示页 (g.co/verifyaccount)", "https://g.co/verifyaccount")
        add_url("▸ 设备配对代码验证 (google.com/device)", "https://www.google.com/device")
        add_separator()
        add_url("◆ 服务条款与归属地 (terms)", "https://policies.google.com/terms")
        add_url("◆ 关联国家改区申请 (country-association-form)", "https://policies.google.com/country-association-form")
        add_url("◆ 年龄验证确认 (age-verification)", "https://myaccount.google.com/age-verification")
        add_url("◆ 账号停用申诉表单 (forms.gle)", "https://forms.gle/hGzM9MEUv2azZsrb9")
        add_url("◆ 关闭支付资料 (payments)", "https://payments.google.com/gp/w/home/reauthprompt?rtrp=/home/settings")
        add_url("× 取消Pro订阅 (deleteservices)", "https://myaccount.google.com/deleteservices")
        add_separator()
        add_url("★ ChatGPT 对话主页", "https://chatgpt.com/")
        add_url("✦ Claude 对话主页", "https://claude.ai/")
        add_url("◆ IP 环境检测 (ipinfo.io)", "https://ipinfo.io/json")
        add_url("◆ WebRTC 防泄漏体检", "https://browserleaks.com/webrtc")

        menu.Show(anchor_btn, Point(0, anchor_btn.Height + 2))
