# T3.5/T3.6 host 单测：不依赖 ESP-IDF，直接用主机 cc 编译。
#
# 为什么能脱离 IDF：
#   - components/prompt_store/prompt_pack.c 是纯内存校验逻辑（只依赖 <stdint.h>/<string.h>），
#     覆盖 assets 分区提示音 pack 的头部/条目/CRC 校验与坏包用例；
#   - components/feedback/include/feedback_policy.h 是 static inline 纯逻辑头，无 IDF 依赖，
#     覆盖「云端错误码→反馈类别」映射（3001/3002 → tts-fail，其余 → cloud-error）。
# 因此不必上板、不必等 idf.py build。
#
# 用法：
#   make -C test/host              # 编译并运行全部用例（推荐）
#   make -C test/host pack         # 只跑 pack 校验用例
#   make -C test/host feedback     # 只跑反馈策略用例
#   make -C test/host clean        # 清掉编译产物

CC       ?= cc
CPPFLAGS += -I../../components/prompt_store/include -I../../components/feedback/include
CFLAGS   ?= -O2 -std=c11 -Wall -Wextra
LDFLAGS  ?=

BINS := feedback_policy_test prompt_pack_test

.PHONY: all run clean feedback pack

all: run

run: $(BINS)
	@echo
	./prompt_pack_test
	@echo
	./feedback_policy_test

pack: prompt_pack_test
	@echo
	./prompt_pack_test

feedback: feedback_policy_test
	@echo
	./feedback_policy_test

prompt_pack_test: prompt_pack_test.c ../../components/prompt_store/prompt_pack.c \
		../../components/prompt_store/include/prompt_pack.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ prompt_pack_test.c \
		../../components/prompt_store/prompt_pack.c $(LDFLAGS)

feedback_policy_test: feedback_policy_test.c ../../components/feedback/include/feedback_policy.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ feedback_policy_test.c $(LDFLAGS)

clean:
	rm -f $(BINS)
