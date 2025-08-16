import os, sys, json, math, threading, shutil, random
from pathlib import Path

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QMessageBox, QListWidgetItem
)

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

# optional proxy helper (nếu không có vẫn chạy bình thường)
try:
    from selenium_authenticated_proxy import SeleniumAuthenticatedProxy
    HAS_PROXY_HELPER = True
except Exception:
    HAS_PROXY_HELPER = False

# auto manage ChromeDriver
from webdriver_manager.chrome import ChromeDriverManager

APP_TITLE = "Chrome Profile Manager — Proxy + Random UA"
CONFIG_FILE = "profiles.json"
DEFAULT_PROFILE_ROOT = str(Path("profiles").absolute())
UA_FILE = "useragents.txt"

# ----------------- Helpers -----------------

def load_profiles():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}  # { name: {"path": "...", "proxy": "...", "ua_mode": "fixed|random", "ua": "..."} }

def save_profiles(data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def detect_chrome_binary():
    # best-effort find chrome binary
    cands = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chrome.exe"),
        shutil.which("google-chrome-stable"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/local/bin/google-chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None

def parse_proxy(proxy_str: str):
    """
    Support:
      - ip:port
      - user:pass@ip:port
      - ip:port:user:pass
    """
    if not proxy_str:
        return {}
    s = proxy_str.strip()
    if "@" in s:
        cred, host = s.split("@", 1)
        user, pwd = cred.split(":", 1)
        ip, port = host.split(":", 1)
        return {"host": ip, "port": port, "user": user, "pass": pwd}
    parts = s.split(":")
    if len(parts) == 2:
        ip, port = parts
        return {"host": ip, "port": port}
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return {"host": ip, "port": port, "user": user, "pass": pwd}
    return {"raw": s}

def make_auth_extension(ext_root, host, port, user, pwd):
    # extension để add auth khi không có selenium_authenticated_proxy
    ext_dir = os.path.join(ext_root, "_proxy_ext")
    os.makedirs(ext_dir, exist_ok=True)
    manifest = """
    {
      "version": "1.0.0",
      "manifest_version": 2,
      "name": "AuthProxy",
      "permissions": ["proxy","tabs","unlimitedStorage","storage","<all_urls>","webRequest","webRequestBlocking"],
      "background": {"scripts": ["background.js"]}
    }
    """
    background = f"""
    var config = {{
        mode: "fixed_servers",
        rules: {{
            singleProxy: {{
                scheme: "http",
                host: "{host}",
                port: parseInt({port})
            }},
            bypassList: ["localhost"]
        }}
    }};
    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
    function callbackFn(details) {{
        return {{
            authCredentials: {{
                username: "{user}",
                password: "{pwd}"
            }}
        }};
    }}
    chrome.webRequest.onAuthRequired.addListener(
        callbackFn,
        {{urls: ["<all_urls>"]}},
        ["blocking"]
    );
    """
    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(manifest.strip())
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background.strip())
    return ext_dir

def load_user_agents():
    ualist = []
    if os.path.exists(UA_FILE):
        with open(UA_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ln = line.strip()
                if ln: ualist.append(ln)
    # fallback 1 số UA phổ biến nếu file trống
    if not ualist:
        ualist = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:118.0) Gecko/20100101 Firefox/118.0",
        ]
    return ualist

UAS = load_user_agents()

def choose_ua(mode: str, fixed_value: str):
    if mode == "fixed" and fixed_value:
        return fixed_value
    return random.choice(UAS)

def compute_grid(n, screen_w, screen_h):
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    win_w = max(600, screen_w // cols)
    win_h = max(500, screen_h // rows)
    pos = []
    for i in range(n):
        c = i % cols
        r = i // cols
        x = c * win_w
        y = r * win_h
        pos.append((x, y, win_w, win_h))
    return pos

def open_chrome(profile_name, profile_path, proxy_str, ua_mode, ua_fixed, position=None, size=None, start_url="https://whatismyipaddress.com/"):
    try:
        user_data_dir = os.path.abspath(profile_path)
        os.makedirs(user_data_dir, exist_ok=True)

        opts = Options()
        opts.add_argument(f"--user-data-dir={user_data_dir}")
        # mỗi profile tách hẳn Default profile
        opts.add_argument(f"--profile-directory=Default")
        opts.add_argument("--disable-features=AutomationControlled")

        # UA
        ua = choose_ua(ua_mode, ua_fixed)
        if ua:
            opts.add_argument(f"--user-agent={ua}")

        # Proxy
        pinfo = parse_proxy(proxy_str)
        if pinfo:
            if "host" in pinfo and "port" in pinfo:
                if pinfo.get("user") and pinfo.get("pass"):
                    if HAS_PROXY_HELPER:
                        helper = SeleniumAuthenticatedProxy(
                            proxy_url=f"http://{pinfo['user']}:{pinfo['pass']}@{pinfo['host']}:{pinfo['port']}"
                        )
                        helper.enrich_chrome_options(opts)
                    else:
                        ext_path = make_auth_extension(user_data_dir, pinfo["host"], pinfo["port"], pinfo["user"], pinfo["pass"])
                        opts.add_argument(f"--load-extension={ext_path}")
                else:
                    opts.add_argument(f"--proxy-server=http://{pinfo['host']}:{pinfo['port']}")
            elif "raw" in pinfo:
                opts.add_argument(f"--proxy-server=http://{pinfo['raw']}")

        # vị trí/kích thước
        if position and size:
            x, y = position
            w, h = size
            opts.add_argument(f"--window-position={x},{y}")
            opts.add_argument(f"--window-size={w},{h}")

        # binary: cho phép selenium tự tìm; nếu bác muốn ép, mở comment dòng dưới
        # chrome_bin = detect_chrome_binary()
        # if chrome_bin: opts.binary_location = chrome_bin

        driver_path = ChromeDriverManager().install()
        driver = webdriver.Chrome(service=Service(driver_path), options=opts)
        driver.get(start_url)
    except Exception as e:
        QtWidgets.QMessageBox.critical(None, "Chrome Error", str(e))

# ----------------- UI -----------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(920, 560)

        self.profiles = load_profiles()
        self.profile_root = DEFAULT_PROFILE_ROOT

        # Widgets
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        lay = QtWidgets.QGridLayout(central)

        # Input
        self.ed_name = QtWidgets.QLineEdit()
        self.ed_proxy = QtWidgets.QLineEdit()
        self.ed_path = QtWidgets.QLineEdit(self.profile_root)
        self.btn_browse = QtWidgets.QPushButton("Chọn thư mục…")
        self.btn_browse.clicked.connect(self.pick_folder)

        # UA
        self.cmb_ua_mode = QtWidgets.QComboBox()
        self.cmb_ua_mode.addItems(["fixed", "random"])
        self.ed_ua_fixed = QtWidgets.QLineEdit()
        self.btn_random_now = QtWidgets.QPushButton("Random UA → ô dưới")
        self.btn_random_now.clicked.connect(self.fill_random_ua)

        # Buttons
        self.btn_add = QtWidgets.QPushButton("Tạo profile")
        self.btn_add.clicked.connect(self.create_profile)
        self.btn_update_proxy = QtWidgets.QPushButton("Cập nhật proxy")
        self.btn_update_proxy.clicked.connect(self.update_proxy)
        self.btn_update_ua = QtWidgets.QPushButton("Cập nhật UA")
        self.btn_update_ua.clicked.connect(self.update_ua)
        self.btn_import_proxy = QtWidgets.QPushButton("Import proxy từ .txt")
        self.btn_import_proxy.clicked.connect(self.import_proxy_txt)
        self.btn_open = QtWidgets.QPushButton("Mở Chrome (song song)")
        self.btn_open.clicked.connect(self.open_selected)
        self.btn_open_no_proxy = QtWidgets.QPushButton("Mở không proxy")
        self.btn_open_no_proxy.clicked.connect(lambda: self.open_selected(use_proxy=False))

        # List
        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.refresh_list()

        # Layout
        r = 0
        lay.addWidget(QtWidgets.QLabel("Tên profile:"), r, 0); lay.addWidget(self.ed_name, r, 1, 1, 3); r+=1
        lay.addWidget(QtWidgets.QLabel("Thư mục chứa profile:"), r, 0); lay.addWidget(self.ed_path, r, 1, 1, 2); lay.addWidget(self.btn_browse, r, 3); r+=1
        lay.addWidget(QtWidgets.QLabel("Proxy (ip:port | user:pass@ip:port | ip:port:user:pass):"), r, 0); lay.addWidget(self.ed_proxy, r, 1, 1, 3); r+=1
        lay.addWidget(QtWidgets.QLabel("User-Agent mode:"), r, 0); lay.addWidget(self.cmb_ua_mode, r, 1)
        lay.addWidget(self.btn_random_now, r, 2); r+=1
        lay.addWidget(QtWidgets.QLabel("UA cố định (nếu chọn fixed):"), r, 0); lay.addWidget(self.ed_ua_fixed, r, 1, 1, 3); r+=1

        btns1 = QtWidgets.QHBoxLayout()
        btns1.addWidget(self.btn_add); btns1.addWidget(self.btn_update_proxy); btns1.addWidget(self.btn_update_ua); btns1.addWidget(self.btn_import_proxy)
        lay.addLayout(btns1, r, 0, 1, 4); r+=1

        lay.addWidget(QtWidgets.QLabel("Danh sách profile (chọn nhiều để mở song song):"), r, 0, 1, 4); r+=1
        lay.addWidget(self.list, r, 0, 1, 4); r+=1

        btns2 = QtWidgets.QHBoxLayout()
        btns2.addWidget(self.btn_open); btns2.addWidget(self.btn_open_no_proxy)
        lay.addLayout(btns2, r, 0, 1, 4)

    # ---- actions ----
    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu profiles", self.ed_path.text() or str(Path.cwd()))
        if d:
            self.ed_path.setText(d)

    def fill_random_ua(self):
        self.ed_ua_fixed.setText(random.choice(UAS))

    def refresh_list(self):
        self.list.clear()
        for name, info in self.profiles.items():
            proxy = info.get("proxy", "")
            ua_mode = info.get("ua_mode", "random")
            ua = info.get("ua", "")
            label = f"{name} | proxy={proxy or '-'} | ua_mode={ua_mode}{'('+ua[:28]+'...)' if ua_mode=='fixed' and ua else ''}"
            item = QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, name)
            self.list.addItem(item)

    def create_profile(self):
        name = self.ed_name.text().strip()
        base = self.ed_path.text().strip() or DEFAULT_PROFILE_ROOT
        if not name:
            QMessageBox.warning(self, "Lỗi", "Tên profile không được trống")
            return
        path = os.path.join(base, name)
        os.makedirs(path, exist_ok=True)
        if name in self.profiles:
            QMessageBox.information(self, "Info", "Profile đã tồn tại, chỉ cập nhật đường dẫn")
        self.profiles[name] = {
            "path": path,
            "proxy": self.ed_proxy.text().strip(),
            "ua_mode": self.cmb_ua_mode.currentText(),
            "ua": self.ed_ua_fixed.text().strip()
        }
        save_profiles(self.profiles)
        self.refresh_list()
        QMessageBox.information(self, "OK", f"Đã tạo/cập nhật profile: {name}")

    def update_proxy(self):
        sel = self.get_selected_names(single=True)
        if not sel: return
        name = sel[0]
        self.profiles[name]["proxy"] = self.ed_proxy.text().strip()
        save_profiles(self.profiles)
        self.refresh_list()
        QMessageBox.information(self, "OK", f"Đã cập nhật proxy cho {name}")

    def update_ua(self):
        sel = self.get_selected_names(single=True)
        if not sel: return
        name = sel[0]
        self.profiles[name]["ua_mode"] = self.cmb_ua_mode.currentText()
        self.profiles[name]["ua"] = self.ed_ua_fixed.text().strip()
        save_profiles(self.profiles)
        self.refresh_list()
        QMessageBox.information(self, "OK", f"Đã cập nhật UA cho {name}")

    def import_proxy_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file proxies.txt", "", "Text files (*.txt)")
        if not path: return
        added = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                proxy = line.strip()
                if not proxy: continue
                # tạo profile tự động
                name = f"profile_{len(self.profiles)+1}"
                pdir = os.path.join(self.ed_path.text().strip() or DEFAULT_PROFILE_ROOT, name)
                os.makedirs(pdir, exist_ok=True)
                self.profiles[name] = {
                    "path": pdir,
                    "proxy": proxy,
                    "ua_mode": "random",
                    "ua": ""
                }
                added += 1
        save_profiles(self.profiles)
        self.refresh_list()
        QMessageBox.information(self, "OK", f"Đã thêm {added} proxy thành {added} profile mới")

    def get_selected_names(self, single=False):
        items = self.list.selectedItems()
        if single and len(items) != 1:
            QMessageBox.warning(self, "Chọn 1 profile", "Hãy chọn đúng 1 profile trong danh sách")
            return None
        return [it.data(QtCore.Qt.UserRole) for it in items]

    def open_selected(self, use_proxy=True):
        names = self.get_selected_names(single=False)
        if not names:
            QMessageBox.information(self, "Info", "Hãy chọn ít nhất 1 profile")
            return
        screen = QApplication.primaryScreen().geometry()
        grid = compute_grid(len(names), screen.width(), screen.height())

        for idx, name in enumerate(names):
            info = self.profiles.get(name, {})
            path = info.get("path")
            proxy = info.get("proxy") if use_proxy else ""
            ua_mode = info.get("ua_mode", "random")
            ua_fixed = info.get("ua", "")
            x, y, w, h = grid[idx]
            threading.Thread(
                target=open_chrome,
                args=(name, path, proxy, ua_mode, ua_fixed, (x, y), (w, h)),
                daemon=True
            ).start()

def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
