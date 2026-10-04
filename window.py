import ctypes
import tkinter as tk
from functools import partial
from math import cos, radians, sin
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageTk

BACKGROUND, PLATE, BUTTON_GREY, SWITCH_OFF = '#1e1f22', '#2b2d31', '#383a40', '#4e5058'
TEXT, DIM_TEXT = '#f2f3f5', '#b5bac1'
GREEN, RED, CLAUDE_ORANGE = '#23a55a', '#f23f43', '#d97757'
BUTTON_CORNER = 0.3
SUPERSAMPLING = 3
PULSE_FRAMES = 12
REFRESH_MILLISECONDS = 40
LONG_TEXT_LENGTH = 160
DWM_DARK_MODE, DWM_CAPTION_COLOR = 20, 35


def blend(color, other, share):
    first, second = (tuple(int(value[index:index + 2], 16) for index in (1, 3, 5)) for value in (color, other))
    return tuple(round(a * share + b * (1 - share)) for a, b in zip(first, second))


def circle(centre, radius):
    return centre - radius, centre - radius, centre + radius, centre + radius


def render(width, height, paint):
    image = Image.new('RGBA', (width * SUPERSAMPLING, height * SUPERSAMPLING), BACKGROUND)
    paint(image, ImageDraw.Draw(image))
    return image.resize((width, height), Image.LANCZOS)


def handset(side, color, lifted):
    layer = Image.new('RGBA', (side, side))
    middle, arc_centre, radius, thickness = side / 2, side * 0.66, side * 0.30, side * 0.13
    outer = radius + thickness / 2
    ImageDraw.Draw(layer).arc((middle - outer, arc_centre - outer, middle + outer, arc_centre + outer),
                              208, 332, fill=color, width=round(thickness))
    for angle, lean in ((208, -1), (332, 1)):
        cap = Image.new('RGBA', (side, side))
        ImageDraw.Draw(cap).rounded_rectangle(
            (middle - side * 0.115, middle - side * 0.085, middle + side * 0.115, middle + side * 0.085),
            radius=side * 0.05, fill=color)
        cap = cap.rotate(-lean * 28, resample=Image.BICUBIC)
        end_x = middle + radius * cos(radians(angle))
        end_y = arc_centre + radius * sin(radians(angle))
        layer.alpha_composite(cap, (round(end_x - middle - lean * side * 0.01), round(end_y - middle + side * 0.035)))
    return layer.rotate(135, resample=Image.BICUBIC) if lifted else layer


def microphone(side, color, crossed_over=None):
    layer = Image.new('RGBA', (side, side))
    draw = ImageDraw.Draw(layer)
    line = round(side * 0.055)
    draw.rounded_rectangle((side * 0.405, side * 0.20, side * 0.595, side * 0.56), radius=side * 0.095, fill=color)
    draw.arc((side * 0.30, side * 0.24, side * 0.70, side * 0.66), 15, 165, fill=color, width=line)
    draw.line((side * 0.5, side * 0.66, side * 0.5, side * 0.77), fill=color, width=line)
    draw.rounded_rectangle((side * 0.39, side * 0.76, side * 0.61, side * 0.76 + line), radius=line / 2, fill=color)
    if crossed_over:
        slash = (side * 0.27, side * 0.21, side * 0.75, side * 0.79)
        draw.line(slash, fill=crossed_over, width=round(side * 0.13))
        draw.line(slash, fill=color, width=round(side * 0.06))
    return layer


def button_image(size, fill, glyph):
    def paint(image, draw):
        draw.rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=image.width * BUTTON_CORNER, fill=fill)
        image.alpha_composite(glyph(image.width))
    return render(size, size, paint)


def switch_image(width, height, on):
    def paint(image, draw):
        draw.rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=image.height / 2,
                               fill=GREEN if on else SWITCH_OFF)
        margin = image.height * 0.08
        knob = image.height - 2 * margin
        left = image.width - margin - knob if on else margin
        draw.ellipse((left, margin, left + knob, margin + knob), fill='white')
    return render(width, height, paint)


def avatar_image(logo, size, pulse=None):
    def paint(image, draw):
        middle, plate_radius = image.width / 2, image.width * 0.34
        if pulse is not None:
            ring_radius = plate_radius + (middle - plate_radius) * pulse
            draw.ellipse(circle(middle, ring_radius), fill=blend(CLAUDE_ORANGE, BACKGROUND, 0.4 * (1 - pulse)))
        draw.ellipse(circle(middle, plate_radius), fill=PLATE)
        mark = logo.resize((round(plate_radius * 1.2),) * 2, Image.LANCZOS)
        image.alpha_composite(mark, ((image.width - mark.width) // 2,) * 2)
    return render(size, size, paint)


def taskbar_icon(logo):
    icon = Image.new('RGBA', (256, 256))
    icon.alpha_composite(logo.resize((256, 256), Image.LANCZOS))
    ImageDraw.Draw(icon).rounded_rectangle((116, 116, 255, 255), radius=140 * BUTTON_CORNER, fill=GREEN)
    icon.alpha_composite(handset(140, 'white', lifted=True), (116, 116))
    return icon


def run(app, from_user):
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Claude.Call')
    root = tk.Tk()
    root.withdraw()
    scale = root.winfo_fpixels('1i') / 96

    def px(points):
        return round(points * scale)

    width, height = px(300), px(500)
    root.title('Звонок Claude')
    root.configure(bg=BACKGROUND)
    root.resizable(False, False)
    root.attributes('-topmost', True)
    root.geometry(f'{width}x{height}+{root.winfo_screenwidth() - width - px(24)}'
                  f'+{root.winfo_screenheight() - height - px(90)}')
    root.protocol('WM_DELETE_WINDOW', app.finish)
    root.bind('<Return>', lambda event: app.answer())
    root.bind('<Escape>', lambda event: app.finish())
    root.update()
    window_handle = ctypes.windll.user32.GetParent(root.winfo_id())
    caption_color = int(BACKGROUND[5:7] + BACKGROUND[3:5] + BACKGROUND[1:3], 16)
    for attribute, value in ((DWM_DARK_MODE, 1), (DWM_CAPTION_COLOR, caption_color)):
        ctypes.windll.dwmapi.DwmSetWindowAttribute(window_handle, attribute, ctypes.byref(ctypes.c_int(value)), 4)

    logo = Image.open(app.HERE / 'logo.png')
    icon = taskbar_icon(logo)
    icons = [ImageTk.PhotoImage(icon.resize((side, side), Image.LANCZOS)) for side in (256, 48, 32, 16)]
    root.iconphoto(True, *icons)

    button_size, avatar_size = px(60), px(150)
    muted_fill = '#%02x%02x%02x' % blend(RED, BACKGROUND, 0.22)
    images = {name: ImageTk.PhotoImage(image) for name, image in {
        'still': avatar_image(logo, avatar_size),
        'answer': button_image(button_size, GREEN, partial(handset, color='white', lifted=True)),
        'hangup': button_image(button_size, RED, partial(handset, color='white', lifted=False)),
        'microphone': button_image(button_size, BUTTON_GREY, partial(microphone, color=TEXT)),
        'muted': button_image(button_size, muted_fill, partial(microphone, color=RED, crossed_over=muted_fill)),
        'locked': button_image(button_size, RED, partial(microphone, color='white', crossed_over=RED)),
        'on': switch_image(px(44), px(26), on=True),
        'off': switch_image(px(44), px(26), on=False),
    }.items()}
    pulse = [ImageTk.PhotoImage(avatar_image(logo, avatar_size, step / PULSE_FRAMES)) for step in range(PULSE_FRAMES)]

    avatar = tk.Label(root, image=images['still'], bg=BACKGROUND)
    avatar.pack(pady=(px(6), 0))
    title = tk.Label(root, text='Звоню Claude…' if from_user else 'Claude звонит Вам',
                     font=('Segoe UI Semibold', 16), fg=TEXT, bg=BACKGROUND)
    title.pack()
    status = tk.Label(root, font=('Segoe UI', 10), fg=DIM_TEXT, bg=BACKGROUND)
    status.pack(pady=(px(2), 0))
    live = tk.Text(root, font=('Segoe UI', 10), fg=DIM_TEXT, bg=BACKGROUND, width=1, height=4, wrap='word', bd=0,
                   highlightthickness=0, padx=px(24), cursor='arrow', state='disabled')
    live.tag_configure('centered', justify='center')
    live.pack(fill='x', pady=(px(8), px(6)))
    buttons = tk.Frame(root, bg=BACKGROUND)
    buttons.pack()

    def round_button(image, caption, command):
        frame = tk.Frame(buttons, bg=BACKGROUND)
        picture = tk.Label(frame, image=images[image], bg=BACKGROUND, cursor='hand2')
        picture.pack()
        picture.bind('<Button-1>', lambda event: command())
        label = tk.Label(frame, text=caption, font=('Segoe UI', 8), fg=DIM_TEXT, bg=BACKGROUND)
        label.pack(pady=(px(4), 0))
        return SimpleNamespace(frame=frame, picture=picture, label=label)

    def toggle_mute():
        app.view['muted'] = not app.view['muted']

    answer_button = round_button('answer', 'Ответить', app.answer)
    mute_button = round_button('microphone', 'Микрофон', toggle_mute)
    hangup_button = round_button('hangup', 'Сбросить', app.finish)
    if not from_user:
        answer_button.frame.pack(side='left', padx=px(18))
    hangup_button.frame.pack(side='left', padx=px(18))

    def switch_row(setting, caption):
        row = tk.Frame(root, bg=BACKGROUND)
        tk.Label(row, text=caption, font=('Segoe UI', 10), fg=TEXT, bg=BACKGROUND).pack(side='left')
        switch = tk.Label(row, image=images['on' if app.settings[setting] else 'off'], bg=BACKGROUND, cursor='hand2')
        switch.pack(side='right')

        def flip(event):
            app.settings[setting] = not app.settings[setting]
            app.save_settings()
            switch.config(image=images['on' if app.settings[setting] else 'off'])

        switch.bind('<Button-1>', flip)
        row.pack(side='bottom', fill='x', padx=px(28), pady=px(6))

    tk.Frame(root, height=px(12), bg=BACKGROUND).pack(side='bottom')
    switch_row('barge_in', 'Перебивать голосом')
    switch_row('fast', 'Быстрый голос')

    if app.DUCKED_VOLUMES_FILE.exists():
        app.duck_other_apps(False)
    others_ducked = not from_user
    if others_ducked:
        app.duck_other_apps(True)
    connected = False
    shown_text = ''
    tick = 0

    def show_live_text(text):
        nonlocal shown_text
        if text == shown_text:
            return
        shown_text = text
        live.config(state='normal', font=('Segoe UI', 9 if len(text) > LONG_TEXT_LENGTH else 10))
        live.delete('1.0', 'end')
        live.insert('1.0', text, 'centered')
        live.config(state='disabled')

    def refresh():
        nonlocal connected, others_ducked, tick
        if others_ducked and app.call_state != 'ringing':
            others_ducked = False
            app.duck_other_apps(False)
        if app.call_state == 'ended':
            return root.destroy()
        tick += 1
        if from_user and app.call_state == 'ringing' and app.voice_ready.is_set():
            app.answer()
        animated = app.call_state == 'ringing' or app.announcer_speaking.is_set()
        avatar.config(image=pulse[tick // 2 % PULSE_FRAMES] if animated else images['still'])
        if app.call_state == 'talking' and not connected:
            connected = True
            title.config(text='Claude')
            answer_button.frame.pack_forget()
            mute_button.frame.pack(side='left', padx=px(18), before=hangup_button.frame)
            hangup_button.label.config(text='Завершить')
        if connected:
            muted, locked = app.view['muted'], app.view['locked']
            mute_button.picture.config(image=images['locked' if locked else 'muted' if muted else 'microphone'])
            mute_button.label.config(text='Микрофон выкл' if muted else 'Микрофон')
            if muted:
                status.config(text='микрофон выключен', fg=RED)
            else:
                status.config(text=app.view['status'], fg=DIM_TEXT)
            show_live_text(app.view['live'])
        root.after(REFRESH_MILLISECONDS, refresh)

    refresh()
    root.deiconify()
    root.mainloop()
