idf_component_register(SRCS "audio_player.c" "decoder_mp3.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "." "third_party/minimp3"
                       PRIV_REQUIRES audio_io esp_timer)
