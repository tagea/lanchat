# PyInstaller: сборка LAN Chat в самостоятельное приложение (без установки Python).
# Windows -> dist/LANChat/LANChat.exe (затем упаковывается установщиком), macOS -> dist/LANChat.app
import sys

a = Analysis(['lanchat.py'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='LANChat', console=False, upx=False,
          icon='assets/icon.ico' if sys.platform == 'win32' else None)
coll = COLLECT(exe, a.binaries, a.datas, name='LANChat', upx=False)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='LANChat.app',
        icon='assets/icon.icns',
        bundle_identifier='com.lanchat.app',
        info_plist={
            # без этого macOS не даст программе видеть локальную сеть
            'NSLocalNetworkUsageDescription':
                'LAN Chat обменивается сообщениями с компьютерами в вашей Wi-Fi сети.',
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': '2.2',
        },
    )
