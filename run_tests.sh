#!/bin/sh
# 在本机或 AstrBot 容器内跑测试。
#
# 用例需要 import main，而 main 依赖 astrbot 运行时；本机没有 astrbot 时
# 用容器执行（把仓库挂载到 /tmp/mfrun 后跑 pytest）。
#
# 用法：
#   ./run_tests.sh                      # 自动：有 astrbot 就本机跑，否则找容器
#   ./run_tests.sh --container astrbot  # 指定容器名
set -eu

CONTAINER="astrbot"
if [ "${1:-}" = "--container" ]; then
    CONTAINER="${2:?用法: $0 --container <容器名>}"
fi

run_local() {
    python3 -m pytest "$@"
}

if python3 -c "import astrbot" >/dev/null 2>&1; then
    echo "[run_tests] 本机已有 astrbot 运行时，直接执行"
    run_local
    exit $?
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "[run_tests] 本机没有 astrbot，也没有 docker，无法执行测试" >&2
    exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "[run_tests] 找不到容器 $CONTAINER，无法执行测试" >&2
    exit 1
fi

echo "[run_tests] 本机没有 astrbot 运行时，改用容器 $CONTAINER 执行"
docker exec "$CONTAINER" rm -rf /tmp/mfrun >/dev/null 2>&1 || true
docker exec "$CONTAINER" mkdir -p /tmp/mfrun
docker cp main.py "$CONTAINER:/tmp/mfrun/main.py" >/dev/null
docker cp pytest.ini "$CONTAINER:/tmp/mfrun/pytest.ini" >/dev/null
docker cp _conf_schema.json "$CONTAINER:/tmp/mfrun/_conf_schema.json" >/dev/null
docker cp tests "$CONTAINER:/tmp/mfrun/tests" >/dev/null
docker exec "$CONTAINER" sh -c 'cd /tmp/mfrun && python -m pytest'
