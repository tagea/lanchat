LAN Chat — сборка готовых приложений (без установки Python)
============================================================

Сборку делает GitHub бесплатно, на своих Windows- и Mac-компьютерах.

1. Зайдите на github.com, нажмите «+» -> New repository.
   Имя: lanchat, тип Private, нажмите Create repository.
2. На странице репозитория: «uploading an existing file».
   Перетащите ВСЮ распакованную папку целиком (вместе со скрытой
   папкой .github — на Mac покажите её клавишами Cmd+Shift+.),
   нажмите Commit changes.
3. Откройте вкладку Actions -> «Build LAN Chat». Через 3-5 минут
   появится зелёная галочка.
4. Нажмите на запуск -> внизу Artifacts:
     LANChat-Windows  -> LANChat.exe для Windows
     LANChat-Mac      -> LANChat.app для Mac

Если папка .github не загрузилась: в репозитории Add file ->
Create new file, имя .github/workflows/build.yml, вставьте
содержимое файла build.yml из этого архива, Commit.
