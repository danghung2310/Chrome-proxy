import os, shutil
import PyInstaller.__main__

# dọn build cũ
for p in ["dist", "build", "main.spec"]:
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.isfile(p):
        os.remove(p)

args = [
    "main.py",
    "--onefile",
    "--noconsole",
    "--name=chrome_profile_manager",
    "--add-data=profiles.json;.",
    "--add-data=useragents.txt;.",
]

PyInstaller.__main__.run(args)
print("✅ Build xong! File .exe nằm trong thư mục dist/")
