#!/bin/sh

cd ${APP_HOME}
. venv/bin/activate
python src/shelly_handler.py $@
