#!/bin/sh
# macOS: двойной клик запускает xui-fleet. Если Python не установлен, предложит установить его.
exec sh "$(dirname "$0")/scripts/start-unix.sh"
