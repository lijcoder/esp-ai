# T3.6 host 单测：不依赖 ESP-IDF，直接用主机 cc 编译（契约 build/t36_contract.md §5.3）。
#
# 为什么能脱离 IDF：
#   - components/assets/*.c 只依赖 <stddef.h>/<stdint.h> 与同目录生成的 prompt_pcm_sizes.h，
#     没有任何 esp_log / FreeRTOS / 驱动依赖；
#   - components/feedback/include/feedback_policy.h 是 static inline 纯逻辑头，无 IDF 依赖。
# 因此可以拿主机 cc 直接验证「提示音资源的时长/峰值/首尾淡入淡出」与「云端错误码→反馈类别」映射，
# 不必上板、不必等 idf.py build。
#
# 用法：
#   make -C test/host              # 编译并运行全部用例（推荐）
#   make -C test/host run          # 同上
#   make -C test/host clean        # 清掉编译产物
#   make -C test/host feedback     # 只跑 feedback_policy 用例（assets 未生成时也能跑）
#
# 注意：prompt_assets.c / prompt_*.c / prompt_pcm_sizes.h 由
# `uv run tools/prompt_gen.py`（prompt-audio 交付）生成；缺失时本 Makefile 会给出明确提示。

CC      ?= cc
CPPFLAGS += -I../../components/assets/include -I../../components/feedback/include
CFLAGS   ?= -O2 -std=c11 -Wall -Wextra
LDFLAGS  ?=

ASSETS_DIR   := ../../components/assets
ASSETS_SRCS  := $(wildcard $(ASSETS_DIR)/*.c)
ASSETS_TABLE := $(ASSETS_DIR)/prompt_assets.c

BINS := feedback_policy_test prompt_assets_test

.PHONY: all run clean feedback check-assets

all: run

run: $(BINS)
	@echo
	./feedback_policy_test
	@echo
	./prompt_assets_test

feedback: feedback_policy_test
	@echo
	./feedback_policy_test

# assets 资源表是否已生成：缺失时给一条可执行的提示，而不是一串 undefined reference
check-assets:
	@test -f "$(ASSETS_TABLE)" || { \
		echo "ERROR: 缺少 $(ASSETS_TABLE)（提示音资源表未生成）" >&2; \
		echo "       先运行: uv run tools/prompt_gen.py" >&2; \
		echo "       或先跑不依赖 assets 的: make -C test/host feedback" >&2; \
		exit 2; \
	}

prompt_assets_test: check-assets prompt_assets_test.c $(ASSETS_SRCS) \
		$(ASSETS_DIR)/include/prompt_assets.h $(ASSETS_DIR)/prompt_pcm_sizes.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ prompt_assets_test.c $(ASSETS_SRCS) $(LDFLAGS)

feedback_policy_test: feedback_policy_test.c ../../components/feedback/include/feedback_policy.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ feedback_policy_test.c $(LDFLAGS)

clean:
	rm -f $(BINS)
