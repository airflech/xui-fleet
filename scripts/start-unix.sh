#!/bin/sh
# macOS and Linux: finds Python 3.9+, offers to install it (with the user's consent) if it is missing,
# then starts xui-fleet. Everything else (the environment and the dependencies) launcher.py installs itself.
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
OS=$(uname -s)

pause_exit() { echo "$1"; printf "Нажмите Enter, чтобы закрыть…"; read -r _ </dev/tty 2>/dev/null; exit 1; }

ok_python() {  # usable: 3.9+ with venv/ensurepip (Debian ships them separately)
  "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    p=$(command -v "$c" 2>/dev/null) || continue
    # On a Mac without developer tools /usr/bin/python3 is only a stub that pops up an installer: skip it here.
    if [ "$OS" = Darwin ] && [ "$p" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if ok_python "$p"; then echo "$p"; return 0; fi
  done
  return 1
}

ask() {
  echo ""
  echo "$1"
  printf "Установить сейчас? [Д/н] "
  read -r answer </dev/tty 2>/dev/null || answer=""
  case "$answer" in [nNнН]*) pause_exit "Установка отменена. Без Python приложение не запустится." ;; esac
}

as_root() {  # the system asks for the password itself: that is the permission
  if [ "$(id -u)" = 0 ]; then "$@"
  elif command -v sudo >/dev/null 2>&1 && [ -t 0 ]; then sudo "$@"
  elif command -v pkexec >/dev/null 2>&1; then pkexec "$@"
  else sudo "$@"; fi
}

install_mac() {
  ask "Для работы xui-fleet нужен Python. На Mac он входит в «Инструменты разработчика» Apple (бесплатно, ~1 ГБ)."
  xcode-select --install >/dev/null 2>&1
  echo "Откроется окно Apple — нажмите «Установить» и дождитесь окончания (обычно 5–15 минут)."
  i=0
  until xcode-select -p >/dev/null 2>&1 && ok_python /usr/bin/python3; do
    sleep 10; i=$((i + 1))
    [ $i -gt 180 ] && pause_exit "Не дождался установки. Когда она закончится, запустите файл ещё раз."
  done
}

install_linux() {
  ask "Для работы xui-fleet нужен Python 3 с модулем venv (из репозитория вашего дистрибутива)."
  if command -v apt-get >/dev/null 2>&1; then
    as_root sh -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip'
  elif command -v dnf >/dev/null 2>&1; then as_root dnf install -y python3 python3-pip
  elif command -v yum >/dev/null 2>&1; then as_root yum install -y python3 python3-pip
  elif command -v pacman >/dev/null 2>&1; then as_root pacman -Sy --noconfirm python python-pip
  elif command -v zypper >/dev/null 2>&1; then as_root zypper --non-interactive install python3 python3-pip
  elif command -v apk >/dev/null 2>&1; then as_root apk add python3 py3-pip
  else pause_exit "Не знаю пакетный менеджер этой системы. Установите Python 3.9+ вручную и запустите снова."
  fi
}

PY=$(find_python)
if [ -z "$PY" ]; then
  if [ "$OS" = Darwin ]; then install_mac; else install_linux; fi
  PY=$(find_python) || pause_exit "Python так и не нашёлся. Запустите файл ещё раз или установите Python 3.9+ вручную."
fi
"$PY" "$ROOT/launcher.py" || pause_exit "Запуск завершился с ошибкой."
