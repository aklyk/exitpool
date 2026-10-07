# Сторонние компоненты

exitpool распространяет в релизе GitHub три готовых бинарника для AWG-выходов. Их исходники закреплены
в `release/versions.env`, сборка воспроизводится скриптом `release/build-binaries.sh`.

| Компонент | Версия (коммит) | Лицензия | Где исходники |
|---|---|---|---|
| amneziawg-go | v3.1.20260828 (`b5928efb6ca19f0153958460c3d141f04abc5c2e`) | MIT | https://github.com/amnezia-vpn/amneziawg-go |
| amneziawg-tools (`awg`) | v3.1.20260812 (`ee0f0a9aa34ff0a0da4b3433b9512781cfe02843`) | GPL-2.0 | https://github.com/amnezia-vpn/amneziawg-tools |
| Xray-core (`xray`) | v26.9.9, официальный архив `Xray-linux-64.zip` | MPL-2.0 | https://github.com/XTLS/Xray-core |
| Go (рантайм в amneziawg-go) | 1.25.14 | BSD-3-Clause | https://go.dev |
| musl libc (в статическом `awg`) | из Ubuntu 24.04 | MIT | https://musl.libc.org |

`awg` распространяется под GPL-2.0: архив точного исходного кода этой версии
(`amneziawg-tools-ee0f0a9aa34ff0a0da4b3433b9512781cfe02843.tar.gz`) приложен к каждому релизу рядом с бинарником.

**Happ Desktop** — проприетарная программа. exitpool её не распространяет: образ для Happ-выходов
собирается на сервере пользователя из официального пакета (версия и SHA-256 закреплены в `happ/Dockerfile`).
