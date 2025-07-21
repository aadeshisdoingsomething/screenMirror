from flask import Flask, render_template, session, request, redirect, url_for
from flask_socketio import SocketIO, emit
import cv2
import numpy as np
import base64
import time
import threading
import mss
import mss.tools
import platform
import configparser
import os
from functools import wraps
from dotenv import load_dotenv
import pyautogui

load_dotenv()

if platform.system() == 'Windows':
    import win32gui, win32con

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', os.urandom(24))
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

FRAME_RATE = 80  # Target: 48 FPS
USE_MSS = True
MAX_WIDTH = 1920
MAX_HEIGHT = 1080

if USE_MSS:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        SCREEN_WIDTH = monitor["width"]
        SCREEN_HEIGHT = monitor["height"]
else:
     SCREEN_WIDTH, SCREEN_HEIGHT = pyautogui.size()

config = configparser.ConfigParser()
config.read('config.ini')
try:
    USERNAME, PASSWORD = config['Credentials']['username'], config['Credentials']['password']
except (KeyError, FileNotFoundError) as e:
    print(f"Error loading config: {e}"); exit(1)

def check_auth(username, password): return username == USERNAME and password == PASSWORD
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session: return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# --- Modified image_stream function ---
def image_stream():
    if USE_MSS:
        sct = mss.mss()
        monitor = sct.monitors[1]
    prev_frame_time = 0

    while socketio.streaming_active:  # Only loop while streaming is active
        start_time = time.time()

        if USE_MSS:
            sct_img = sct.grab(monitor)
            frame = np.array(sct_img)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        else:
            frame = pyautogui.screenshot()
            frame = np.array(frame)
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        height, width = frame.shape[:2]

        if width > MAX_WIDTH or height > MAX_HEIGHT:
            scale = min(MAX_WIDTH / width, MAX_HEIGHT / height)
            new_width = int(width * scale)
            new_height = int(height * scale)
            frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)

        _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        jpg_as_text = base64.b64encode(buffer).decode('utf-8')

        new_frame_time = time.time()
        fps = 1 / (new_frame_time - prev_frame_time)
        prev_frame_time = new_frame_time
        # print(f"FPS: {fps:.2f}")

        height, width = frame.shape[:2]
        socketio.emit('image_frame', {'data': jpg_as_text, 'width': width, 'height': height}, namespace='/')

        elapsed_time = time.time() - start_time
        sleep_time = max(0, 1 / FRAME_RATE - elapsed_time)
        time.sleep(sleep_time)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username, password = request.form['username'], request.form['password']
        if check_auth(username, password):
            session['username'] = username
            session.permanent = False  # Session cookie (log out on browser close)
            return redirect(request.form.get('next') or url_for('index'))
        else:
            return '''
                <form method="post">
                    Username: <input type="text" name="username"><br>
                    Password: <input type="password" name="password">
                    <input type="hidden" name="next" value="{}">
                    <br>
                    <input type="submit" value="Login">
                </form>
                <p style="color: red;">Invalid username or password.</p>
            '''.format(request.args.get('next', ''))
    return '''
        <form method="post">
            Username: <input type="text" name="username"><br>
            Password: <input type="password" name="password">
            <input type="hidden" name="next" value="{}">
            <br>
            <input type="submit" value="Login">
        </form>
    '''.format(request.args.get('next', ''))

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

# --- Modified connection handling ---
@socketio.on('connect')
@login_required
def on_connect():
    print('Client connected')
    if not getattr(socketio, 'streaming_thread', None):
        print("Starting image stream thread...")
        socketio.streaming_active = True  # Set streaming to active
        socketio.streaming_thread = threading.Thread(target=image_stream)
        socketio.streaming_thread.daemon = True
        socketio.streaming_thread.start()
    else:
        print("Image stream thread already running")

@socketio.on('disconnect')
def on_disconnect():
    print('Client disconnected')
    socketio.streaming_active = False  # Set streaming to inactive
    if getattr(socketio, 'streaming_thread', None):
        socketio.streaming_thread.join(timeout=1.0) #try to shut down cleanly
        socketio.streaming_thread = None
@socketio.on('click')
@login_required
def handle_click(message):
    x, y = int(message['x']), int(message['y'])
    pyautogui.click(x, y, button=message['button'])

@socketio.on('move')
@login_required
def handle_move(message):
    x, y = int(message['x']), int(message['y'])
    pyautogui.moveTo(x, y)

@socketio.on('scroll')
@login_required
def handle_scroll(message):
    try:
        dy = round(float(message['dy']))
    except ValueError:
        print(f"Invalid scroll value: {message['dy']}"); return

    try:
        if platform.system() == 'Windows':
            hwnd = win32gui.GetForegroundWindow()
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.1)
        elif platform.system() == 'Darwin':
            import subprocess
            subprocess.run(['osascript', '-e', 'tell application "System Events" to tell process "YourAppName" to set frontmost to true'])
            time.sleep(0.1)
        elif platform.system() == 'Linux':
            import subprocess
            subprocess.run(['xdotool', 'windowactivate', '$(xdotool getactivewindow)'])
            time.sleep(0.1)
        else:
            print("Unsupported OS for window activation")
            return

        if dy > 0:  pyautogui.press('pagedown')
        else:       pyautogui.press('pageup')
        #pyautogui.scroll(dy) # Keep for mouse.
        print(f"Scrolling by: {dy}")

    except Exception as e:
        print(f"Error during scrolling: {e}")

@socketio.on('key')
@login_required
def handle_key(message):
    key, code, shift, ctrl, alt = message.get('key'), message.get('code'), message.get('shift') == 'true', message.get('ctrl') == 'true', message.get('alt') == 'true'
    modifiers = ['ctrl'] if ctrl else []
    modifiers.extend(['shift'] if shift else [])
    modifiers.extend(['alt'] if alt else [])
    special_keys = {'Backspace': 'backspace', 'Delete': 'delete', 'Enter': 'enter', 'Tab': 'tab', 'Escape': 'esc', 'ArrowLeft': 'left', 'ArrowRight': 'right', 'ArrowUp': 'up', 'ArrowDown': 'down', 'Space': 'space', 'CapsLock': 'capslock', 'Shift': 'shift', 'Control': 'ctrl', 'Alt': 'alt'}
    if key in special_keys:
        pyautogui.hotkey(*modifiers, special_keys[key]) if modifiers else pyautogui.press(special_keys[key])
    else:
        pyautogui.hotkey(*modifiers, key) if modifiers else pyautogui.write(key)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)