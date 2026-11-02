idf_component_register(SRCS "prompt_store.c" "prompt_pack.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES esp_partition spi_flash heap)
