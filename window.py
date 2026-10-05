import ctypes
import re
import tkinter as tk
from functools import partial
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageTk

BACKGROUND, PLATE, BUTTON_GREY, SWITCH_OFF = '#1e1f22', '#2b2d31', '#383a40', '#4e5058'
TEXT, DIM_TEXT = '#f2f3f5', '#b5bac1'
GREEN, RED, CLAUDE_ORANGE = '#23a55a', '#f23f43', '#d97757'
BASE_WIDTH, BASE_HEIGHT = 300, 516
ZOOM_RANGE = (0.7, 1.8)
BUTTON_CORNER = 0.3
AVATAR_RADIUS = 0.34
SPEAKING_RING_RADIUS = 0.37
SUPERSAMPLING = 3
PULSE_FRAMES = 12
SWITCH_FRAMES = 6
SWITCH_FRAME_MILLISECONDS = 16
REFRESH_MILLISECONDS = 40
RESIZE_SETTLE_MILLISECONDS = 150
LONG_TEXT_LENGTH = 160
DWM_DARK_MODE, DWM_CAPTION_COLOR = 20, 35

STRINGS = {
    'en': {
        'window': 'Claude call', 'calling_claude': 'Calling Claude…', 'claude_calling': 'Claude is calling you',
        'answer': 'Answer', 'decline': 'Decline', 'end': 'End call',
        'microphone': 'Microphone', 'microphone_off': 'Microphone off',
        'fast_voice': 'Fast voice', 'barge_in': 'Interrupt by voice',
        'listening': 'listening', 'hearing': 'listening…', 'recognizing': 'recognizing…',
        'thinking': 'Claude is thinking…', 'speaking': 'Claude is speaking…',
    },
    'ru': {
        'window': 'Звонок Claude', 'calling_claude': 'Звоню Claude…', 'claude_calling': 'Claude звонит Вам',
        'answer': 'Ответить', 'decline': 'Сбросить', 'end': 'Завершить',
        'microphone': 'Микрофон', 'microphone_off': 'Микрофон выкл',
        'fast_voice': 'Быстрый голос', 'barge_in': 'Перебивать голосом',
        'listening': 'слушаю', 'hearing': 'слушаю…', 'recognizing': 'распознаю…',
        'thinking': 'Claude думает…', 'speaking': 'Claude говорит…',
    },
}

ICON_VIEWBOX = 960
ICON_CURVE_STEPS = 8
ICON_PATHS = {
    'microphone': 'M480-400q-50 0-85-35t-35-85v-240q0-50 35-85t85-35q50 0 85 35t35 85v240q0 50-35 85t-85 35Zm-40 '
                  '240v-83q-92-13-157.5-78T203-479q-2-17 9-29t28-12q17 0 28.5 11.5T284-480q14 70 69.5 115T480-320q72 '
                  '0 127-45.5T676-480q4-17 15.5-28.5T720-520q17 0 28 12t9 29q-14 91-79 157t-158 79v83q0 17-11.5 '
                  '28.5T480-120q-17 0-28.5-11.5T440-160Z',
    'microphone_off': 'M672-377q-14-8-18-24.5t4-30.5q7-11 11.5-23.5T676-481q4-17 15.5-28t28.5-11q17 0 28 12t9 29q-3 '
                      '23-10.5 45T727-392q-8 14-24.5 18.5T672-377ZM532-542 383-691q-11-11-17-25.5t-6-30.5v-13q0-50 '
                      '35-85t85-35q50 0 85 35t35 85v189q0 27-24.5 37.5T532-542Zm-92 382v-84q-92-12-157.5-77T203-479q'
                      '-2-17 9-29t28-12q17 0 28.5 11.5T284-480q14 70 69.5 115T480-320q34 0 64.5-10.5T600-360l57 57q'
                      '-29 23-63.5 38.5T520-244v84q0 17-11.5 28.5T480-120q-17 0-28.5-11.5T440-160Zm324 76L84-764q-11'
                      '-11-11-28t11-28q11-11 28-11t28 11l680 680q11 11 11 28t-11 28q-11 11-28 11t-28-11Z',
    'call': 'M798-120q-125 0-247-54.5T329-329Q229-429 174.5-551T120-798q0-18 12-30t30-12h162q14 0 25 9.5t13 22.5l26 '
            '140q2 16-1 27t-11 19l-97 98q20 37 47.5 71.5T387-386q31 31 65 57.5t72 48.5l94-94q9-9 23.5-13.5T670-390l'
            '138 28q14 4 23 14.5t9 23.5v162q0 18-12 30t-30 12Z',
    'call_end': 'M480-640q118 0 232.5 47.5T916-450q12 12 12 28t-12 28l-92 90q-11 11-25.5 12t-26.5-8l-116-88q-8-6-12'
                '-14t-4-18v-114q-38-12-78-19t-82-7q-42 0-82 7t-78 19v114q0 10-4 18t-12 14l-116 88q-12 9-26.5 8T136'
                '-304l-92-90q-12-12-12-28t12-28q88-95 203-142.5T480-640Z',
}


def blend(color, other, share):
    first, second = (tuple(int(value[index:index + 2], 16) for index in (1, 3, 5)) for value in (color, other))
    return tuple(round(a * share + b * (1 - share)) for a, b in zip(first, second))


def circle(centre, radius):
    return centre - radius, centre - radius, centre + radius, centre + radius


def render(width, height, paint):
    image = Image.new('RGBA', (width * SUPERSAMPLING, height * SUPERSAMPLING), BACKGROUND)
    paint(image, ImageDraw.Draw(image))
    return image.resize((width, height), Image.LANCZOS)


def icon_outlines(path):
    tokens = re.findall(r'[A-Za-z]|-?\d*\.?\d+', path)
    outlines, position, start, control, command, index = [], (0.0, 0.0), (0.0, 0.0), None, '', 0

    def numbers(count):
        nonlocal index
        index += count
        return [float(token) for token in tokens[index - count:index]]

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
        kind = command.upper()
        base_x, base_y = position if command.islower() else (0.0, 0.0)
        if kind == 'Z':
            position, control = start, None
        elif kind == 'M':
            x, y = numbers(2)
            position = start = (base_x + x, base_y + y)
            outlines.append([position])
            command, control = 'l' if command.islower() else 'L', None
        elif kind in 'LHV':
            x, y = numbers(2) if kind == 'L' else (numbers(1)[0], None) if kind == 'H' else (None, numbers(1)[0])
            position = (position[0] if x is None else base_x + x, position[1] if y is None else base_y + y)
            outlines[-1].append(position)
            control = None
        elif kind in 'QT':
            if kind == 'Q':
                control_x, control_y, x, y = numbers(4)
                control = (base_x + control_x, base_y + control_y)
            else:
                x, y = numbers(2)
                control = (2 * position[0] - control[0], 2 * position[1] - control[1]) if control else position
            target = (base_x + x, base_y + y)
            for step in range(1, ICON_CURVE_STEPS + 1):
                done = step / ICON_CURVE_STEPS
                left = 1 - done
                outlines[-1].append(tuple(left * left * a + 2 * left * done * b + done * done * c
                                          for a, b, c in zip(position, control, target)))
            position = target
        else:
            raise ValueError(f'icon outline command {command} is not supported')
    return outlines


def icon(name, side, color, share=0.56):
    layer = Image.new('RGBA', (side, side))
    draw = ImageDraw.Draw(layer)
    size, margin = side * share, side * (1 - share) / 2
    for outline in icon_outlines(ICON_PATHS[name]):
        draw.polygon([(margin + x / ICON_VIEWBOX * size, margin + (y + ICON_VIEWBOX) / ICON_VIEWBOX * size)
                      for x, y in outline], fill=color)
    return layer


def button_image(size, fill, glyph):
    def paint(image, draw):
        draw.rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=image.width * BUTTON_CORNER, fill=fill)
        image.alpha_composite(glyph(side=image.width))
    return render(size, size, paint)


def switch_image(width, height, turned_on):
    def paint(image, draw):
        draw.rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=image.height / 2,
                               fill=blend(GREEN, SWITCH_OFF, turned_on))
        margin = image.height * 0.08
        knob = image.height - 2 * margin
        left = margin + (image.width - 2 * margin - knob) * turned_on
        draw.ellipse((left, margin, left + knob, margin + knob), fill='white')
    return render(width, height, paint)


def avatar_image(logo, size, halo_radius=None, halo_color=None):
    def paint(image, draw):
        middle = image.width / 2
        if halo_radius:
            draw.ellipse(circle(middle, image.width * halo_radius), fill=halo_color)
        draw.ellipse(circle(middle, image.width * AVATAR_RADIUS), fill=PLATE)
        mark = logo.resize((round(image.width * AVATAR_RADIUS * 1.2),) * 2, Image.LANCZOS)
        image.alpha_composite(mark, ((image.width - mark.width) // 2,) * 2)
    return render(size, size, paint)


def ringing_avatar(logo, size, progress):
    return avatar_image(logo, size, AVATAR_RADIUS + (0.5 - AVATAR_RADIUS) * progress,
                        blend(CLAUDE_ORANGE, BACKGROUND, 0.4 * (1 - progress)))


def taskbar_icon(logo):
    image = Image.new('RGBA', (256, 256))
    image.alpha_composite(logo.resize((256, 256), Image.LANCZOS))
    ImageDraw.Draw(image).rounded_rectangle((116, 116, 255, 255), radius=140 * BUTTON_CORNER, fill=GREEN)
    image.alpha_composite(icon('call', 140, 'white', share=0.62), (116, 116))
    return image


def run(app, from_user):
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Claude.Call')
    root = tk.Tk()
    root.withdraw()
    screen_scale = root.winfo_fpixels('1i') / 96
    logo = Image.open(app.HERE / 'logo.png')
    ui = SimpleNamespace(zoom=min(max(app.settings['zoom'], ZOOM_RANGE[0]), ZOOM_RANGE[1]),
                         resize_timer=None, others_ducked=not from_user, tick=0)

    def px(points):
        return round(points * screen_scale * ui.zoom)

    def font(size, family='Segoe UI'):
        return family, max(round(size * ui.zoom), 6)

    root.configure(bg=BACKGROUND)
    root.attributes('-topmost', True)
    root.minsize(*(round(side * screen_scale * ZOOM_RANGE[0]) for side in (BASE_WIDTH, BASE_HEIGHT)))
    root.maxsize(*(round(side * screen_scale * ZOOM_RANGE[1]) for side in (BASE_WIDTH, BASE_HEIGHT)))
    root.aspect(BASE_WIDTH, BASE_HEIGHT, BASE_WIDTH, BASE_HEIGHT)
    width, height = px(BASE_WIDTH), px(BASE_HEIGHT)
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
    taskbar_image = taskbar_icon(logo)
    taskbar_images = [ImageTk.PhotoImage(taskbar_image.resize((side, side), Image.LANCZOS))
                      for side in (256, 48, 32, 16)]
    root.iconphoto(True, *taskbar_images)

    def choose_language(code):
        if code != app.settings['language']:
            app.set_language(code)
            build()

    def toggle_mute():
        if not app.view['locked']:
            app.view['muted'] = not app.view['muted']

    def call_button(parent, image, caption, command):
        frame = tk.Frame(parent, bg=BACKGROUND)
        picture = tk.Label(frame, image=ui.images[image], bg=BACKGROUND, cursor='hand2')
        picture.pack()
        picture.bind('<Button-1>', lambda event: command())
        label = tk.Label(frame, text=caption, font=font(8), fg=DIM_TEXT, bg=BACKGROUND)
        label.pack(pady=(px(4), 0))
        return SimpleNamespace(frame=frame, picture=picture, label=label)

    def switch_row(setting, caption):
        row = tk.Frame(root, bg=BACKGROUND)
        tk.Label(row, text=caption, font=font(10), fg=TEXT, bg=BACKGROUND).pack(side='left')
        frame = SWITCH_FRAMES if app.settings[setting] else 0
        switch = tk.Label(row, image=ui.switch_frames[frame], bg=BACKGROUND, cursor='hand2')
        switch.pack(side='right')

        def slide():
            nonlocal frame
            target = SWITCH_FRAMES if app.settings[setting] else 0
            if frame != target and switch.winfo_exists():
                frame += 1 if target > frame else -1
                switch.config(image=ui.switch_frames[frame])
                root.after(SWITCH_FRAME_MILLISECONDS, slide)

        def flip(event):
            app.save_setting(setting, not app.settings[setting])
            slide()

        switch.bind('<Button-1>', flip)
        row.pack(side='bottom', fill='x', padx=px(28), pady=px(6))

    def build():
        for child in root.winfo_children():
            child.destroy()
        ui.strings = STRINGS[app.settings['language']]
        ui.connected = False
        ui.shown_text = None
        root.title(f"{ui.strings['window']} - {app.session_name}")

        button_size, avatar_size = px(60), px(150)
        muted_fill = '#%02x%02x%02x' % blend(RED, BACKGROUND, 0.22)
        ui.images = {name: ImageTk.PhotoImage(image) for name, image in {
            'still': avatar_image(logo, avatar_size),
            'speaking': avatar_image(logo, avatar_size, SPEAKING_RING_RADIUS, GREEN),
            'answer': button_image(button_size, GREEN, partial(icon, 'call', color='white')),
            'hangup': button_image(button_size, RED, partial(icon, 'call_end', color='white')),
            'microphone': button_image(button_size, BUTTON_GREY, partial(icon, 'microphone', color=TEXT)),
            'muted': button_image(button_size, muted_fill, partial(icon, 'microphone_off', color=RED)),
            'locked': button_image(button_size, RED, partial(icon, 'microphone_off', color='white')),
        }.items()}
        ui.pulse = [ImageTk.PhotoImage(ringing_avatar(logo, avatar_size, step / PULSE_FRAMES))
                    for step in range(PULSE_FRAMES)]
        ui.switch_frames = [ImageTk.PhotoImage(switch_image(px(44), px(26), step / SWITCH_FRAMES))
                            for step in range(SWITCH_FRAMES + 1)]

        languages = tk.Frame(root, bg=BACKGROUND)
        languages.place(relx=1, x=-px(12), y=px(8), anchor='ne')
        for code in app.LANGUAGES:
            chosen = code == app.settings['language']
            label = tk.Label(languages, text=code.upper(), font=font(8, 'Segoe UI Semibold'),
                             fg=TEXT if chosen else DIM_TEXT, bg=BACKGROUND, cursor='hand2')
            label.pack(side='left', padx=px(3))
            label.bind('<Button-1>', lambda event, code=code: choose_language(code))

        ui.avatar = tk.Label(root, image=ui.images['still'], bg=BACKGROUND)
        ui.avatar.pack(pady=(px(6), 0))
        ui.title = tk.Label(root, text=ui.strings['calling_claude' if from_user else 'claude_calling'],
                            font=font(16, 'Segoe UI Semibold'), fg=TEXT, bg=BACKGROUND)
        ui.title.pack()
        tk.Label(root, text=app.session_name, font=font(8), fg=DIM_TEXT, bg=BACKGROUND).pack()
        ui.status = tk.Label(root, font=font(10), fg=DIM_TEXT, bg=BACKGROUND)
        ui.status.pack(pady=(px(2), 0))
        ui.live = tk.Text(root, font=font(10), fg=DIM_TEXT, bg=BACKGROUND, width=1, height=4, wrap='word', bd=0,
                          highlightthickness=0, padx=px(24), cursor='arrow', state='disabled')
        ui.live.tag_configure('centered', justify='center')
        ui.live.pack(fill='x', pady=(px(8), px(6)))
        buttons = tk.Frame(root, bg=BACKGROUND)
        buttons.pack()
        ui.answer_button = call_button(buttons, 'answer', ui.strings['answer'], app.answer)
        ui.mute_button = call_button(buttons, 'microphone', ui.strings['microphone'], toggle_mute)
        ui.hangup_button = call_button(buttons, 'hangup', ui.strings['decline'], app.finish)
        if not from_user:
            ui.answer_button.frame.pack(side='left', padx=px(18))
        ui.hangup_button.frame.pack(side='left', padx=px(18))

        tk.Frame(root, height=px(12), bg=BACKGROUND).pack(side='bottom')
        switch_row('barge_in', ui.strings['barge_in'])
        switch_row('fast', ui.strings['fast_voice'])

    def show_live_text(text):
        if text == ui.shown_text:
            return
        ui.shown_text = text
        ui.live.config(state='normal', font=font(9 if len(text) > LONG_TEXT_LENGTH else 10))
        ui.live.delete('1.0', 'end')
        ui.live.insert('1.0', text, 'centered')
        ui.live.config(state='disabled')

    def refresh():
        if ui.others_ducked and app.call_state != 'ringing':
            ui.others_ducked = False
            app.duck_other_apps(False)
        if app.call_state == 'ended':
            return root.destroy()
        ui.tick += 1
        if from_user and app.call_state == 'ringing' and app.voice_ready.is_set():
            app.answer()
        if app.call_state == 'ringing':
            ui.avatar.config(image=ui.pulse[ui.tick // 2 % PULSE_FRAMES])
        else:
            ui.avatar.config(image=ui.images['speaking' if app.announcer_speaking.is_set() else 'still'])
        if app.call_state == 'talking' and not ui.connected:
            ui.connected = True
            ui.title.config(text='Claude')
            ui.answer_button.frame.pack_forget()
            ui.mute_button.frame.pack(side='left', padx=px(18), before=ui.hangup_button.frame)
            ui.hangup_button.label.config(text=ui.strings['end'])
        if ui.connected:
            muted, locked = app.view['muted'], app.view['locked']
            ui.mute_button.picture.config(image=ui.images['locked' if locked else 'muted' if muted else 'microphone'])
            ui.mute_button.label.config(text=ui.strings['microphone_off' if muted else 'microphone'])
            ui.status.config(text=ui.strings.get(app.view['status'], ''))
            show_live_text(app.view['live'])
        root.after(REFRESH_MILLISECONDS, refresh)

    def apply_new_size():
        ui.resize_timer = None
        zoom = min(max(root.winfo_width() / (BASE_WIDTH * screen_scale), ZOOM_RANGE[0]), ZOOM_RANGE[1])
        if abs(zoom - ui.zoom) > 0.02:
            ui.zoom = zoom
            app.save_setting('zoom', round(zoom, 2))
            build()
            root.geometry(f'{px(BASE_WIDTH)}x{px(BASE_HEIGHT)}')

    def window_resized(event):
        if event.widget is root:
            if ui.resize_timer:
                root.after_cancel(ui.resize_timer)
            ui.resize_timer = root.after(RESIZE_SETTLE_MILLISECONDS, apply_new_size)

    if app.DUCKED_VOLUMES_FILE.exists():
        app.duck_other_apps(False)
    if ui.others_ducked:
        app.duck_other_apps(True)
    build()
    refresh()
    root.bind('<Configure>', window_resized)
    root.deiconify()
    root.mainloop()
