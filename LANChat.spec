# PyInstaller: сборка LAN Chat в самостоятельное приложение (без установки Python).
# Windows -> dist/LANChat/LANChat.exe, macOS -> dist/LANChat.app
import sys

a = Analysis(['lanchat.py'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='LANChat', console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='LANChat', upx=False)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='LANChat.app',
        bundle_identifier='com.lanchat.app',
        info_plist={
            # без этого macOS не даст программе видеть локальную сеть
            'NSLocalNetworkUsageDescription':
                'LAN Chat обменивается сообщениями с компьютерами в вашей Wi-Fi сети.',
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': '2.0',
        },
    )
