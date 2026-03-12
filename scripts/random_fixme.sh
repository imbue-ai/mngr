#!/usr/bin/env bash
set -euo pipefail

( grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME0:' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME0[attempts=1]' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME1:' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME1[attempts=1]' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME2:' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME2[attempts=1]' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME3:' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' -P '# FIXME3[attempts=1]' -r $1 || \
  grep --exclude-dir=htmlcov --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules --exclude='random_fixme.sh' --exclude='*.md' -P '# FIXME:' -r $1 ) | shuf -n 1
