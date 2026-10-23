/* run.c - minimal headless libretro frontend for tools/emu/emulate.sh: run a ROM, dump frames (PPM) and RAM. */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include "libretro.h"

static unsigned fmt = RETRO_PIXEL_FORMAT_0RGB1555, fw, fh;
static unsigned char frame[1024 * 1024 * 4];
static size_t fpitch;
static long audio_frames, audio_nonzero;
static FILE *wav;
static long wav_frames;
static const char *sysdir;

static void logcb(enum retro_log_level l, const char *f, ...) {
  va_list a; va_start(a, f); fprintf(stderr, "[core] "); vfprintf(stderr, f, a); va_end(a);
}
static bool env(unsigned cmd, void *data) {
  switch (cmd) {
  case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT: fmt = *(enum retro_pixel_format *)data; return true;
  case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
  case RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY: *(const char **)data = sysdir; return true;
  case RETRO_ENVIRONMENT_GET_LOG_INTERFACE: ((struct retro_log_callback *)data)->log = logcb; return true;
  case RETRO_ENVIRONMENT_GET_CAN_DUPE: *(bool *)data = true; return true;
  case RETRO_ENVIRONMENT_GET_VARIABLE: {
    /* without answers the core maps no pad buttons at all: give it the
       standard RetroPad -> Jaguar map for player 1 */
    static const char *map[][2] = {
      { "_retropad_up", "up" }, { "_retropad_down", "down" }, { "_retropad_left", "left" },
      { "_retropad_right", "right" }, { "_retropad_a", "btn_a" }, { "_retropad_b", "btn_b" },
      { "_retropad_y", "btn_c" }, { "_retropad_select", "pause" }, { "_retropad_start", "option" },
      { "_retropad_x", "num_0" } };
    struct retro_variable *v = data;
    if (!v || !v->key || strncmp(v->key, "virtualjaguar_p1", 16)) return false;
    for (unsigned i = 0; i < sizeof map / sizeof map[0]; i++)
      if (!strcmp(v->key + 16, map[i][0])) { v->value = map[i][1]; return true; }
    return false;
  }
  default: return false;
  }
}
static void video(const void *d, unsigned w, unsigned h, size_t pitch) {
  if (!d) return;
  fw = w; fh = h; fpitch = pitch;
  for (unsigned y = 0; y < h; y++) memcpy(frame + y * pitch, (const char *)d + y * pitch, pitch);
}
static void audio1(int16_t l, int16_t r) { audio_frames++; if (l || r) audio_nonzero++; }
static size_t audiob(const int16_t *d, size_t n) {
  for (size_t i = 0; i < n; i++) if (d[2 * i] || d[2 * i + 1]) audio_nonzero++;
  audio_frames += n;
  if (wav) { fwrite(d, 4, n, wav); wav_frames += n; }
  return n;
}
static unsigned pad;     /* bit n = RETRO_DEVICE_ID_JOYPAD n held, player 1 */
static void poll(void) {}
static int16_t input(unsigned p, unsigned dev, unsigned idx, unsigned id) {
  if (p || dev != RETRO_DEVICE_JOYPAD) return 0;
  if (id == RETRO_DEVICE_ID_JOYPAD_MASK) return (int16_t)pad;
  return id < 16 && (pad >> id) & 1;
}

static void dump(const char *path) {
  FILE *f = fopen(path, "wb"); fprintf(f, "P6\n%u %u\n255\n", fw, fh);
  for (unsigned y = 0; y < fh; y++) for (unsigned x = 0; x < fw; x++) {
    unsigned char rgb[3];
    if (fmt == RETRO_PIXEL_FORMAT_XRGB8888) {
      unsigned v = *(unsigned *)(frame + y * fpitch + 4 * x);
      rgb[0] = v >> 16; rgb[1] = v >> 8; rgb[2] = v;
    } else {
      unsigned v = *(unsigned short *)(frame + y * fpitch + 2 * x);
      if (fmt == RETRO_PIXEL_FORMAT_RGB565) { rgb[0] = (v >> 11) << 3; rgb[1] = ((v >> 5) & 63) << 2; rgb[2] = (v & 31) << 3; }
      else { rgb[0] = ((v >> 10) & 31) << 3; rgb[1] = ((v >> 5) & 31) << 3; rgb[2] = (v & 31) << 3; }
    }
    fwrite(rgb, 1, 3, f);
  }
  fclose(f);
}

/* control mode (outprefix "-"): commands on stdin, one per line
     run N MASK     run N frames holding MASK (retropad bits), reply "ok"
     peek ADDR LEN  reply LEN bytes of Jaguar RAM in hex
     poke ADDR HEX  write bytes into Jaguar RAM, reply "ok"
     shot PATH      save the last frame as a PPM, reply "ok"
     wav PATH       start (or with "-", stop) writing audio to a WAV file
     quit                                                                  */
static void wav_close(void) {
  if (!wav) return;
  unsigned data = wav_frames * 4, riff = 36 + data;
  fseek(wav, 4, SEEK_SET); fwrite(&riff, 4, 1, wav);
  fseek(wav, 40, SEEK_SET); fwrite(&data, 4, 1, wav);
  fclose(wav); wav = NULL;
}
static void wav_open(const char *path, unsigned rate) {
  wav_close();
  wav = fopen(path, "wb"); wav_frames = 0;
  unsigned char h[44] = "RIFF\0\0\0\0WAVEfmt \x10\0\0\0\x01\0\x02\0";
  unsigned br = rate * 4;
  memcpy(h + 24, &rate, 4); memcpy(h + 28, &br, 4);
  h[32] = 4; h[34] = 16; memcpy(h + 36, "data", 4);
  fwrite(h, 1, 44, wav);
}

int main(int argc, char **argv) {
  if (argc < 6) { fprintf(stderr, "usage: run core.so rom.j64 sysdir frames outprefix [every]\n"
                                  "       run core.so rom.j64 sysdir 0 -   (control mode)\n"); return 2; }
  sysdir = argv[3];
  int frames = atoi(argv[4]), every = argc > 6 ? atoi(argv[6]) : frames;
  void *h = dlopen(argv[1], RTLD_NOW);
  if (!h) { fprintf(stderr, "%s\n", dlerror()); return 1; }
#define SYM(n) typeof(&n) p_##n = dlsym(h, #n)
  SYM(retro_set_environment); SYM(retro_set_video_refresh); SYM(retro_set_audio_sample);
  SYM(retro_set_audio_sample_batch); SYM(retro_set_input_poll); SYM(retro_set_input_state);
  SYM(retro_init); SYM(retro_load_game); SYM(retro_get_memory_data); SYM(retro_get_memory_size); SYM(retro_run); SYM(retro_deinit); SYM(retro_unload_game);
  p_retro_set_environment(env); p_retro_init();
  p_retro_set_video_refresh(video); p_retro_set_audio_sample(audio1); p_retro_set_audio_sample_batch(audiob);
  p_retro_set_input_poll(poll); p_retro_set_input_state(input);
  FILE *f = fopen(argv[2], "rb"); fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
  void *rom = malloc(n); fread(rom, 1, n, f); fclose(f);
  struct retro_game_info gi = { argv[2], rom, (size_t)n, NULL };
  if (!p_retro_load_game(&gi)) { fprintf(stderr, "load_game failed\n"); return 1; }
  if (!strcmp(argv[5], "-")) {
    unsigned char *ram = p_retro_get_memory_data(RETRO_MEMORY_SYSTEM_RAM);
    size_t ramsz = p_retro_get_memory_size(RETRO_MEMORY_SYSTEM_RAM);
    char line[4096];
    setvbuf(stdout, NULL, _IOLBF, 0);
    while (fgets(line, sizeof line, stdin)) {
      char cmd[16], arg[2048]; long a = 0, b = 0;
      arg[0] = 0;
      if (sscanf(line, "%15s", cmd) != 1) continue;
      if (!strcmp(cmd, "run") && sscanf(line, "%*s %ld %ld", &a, &b) == 2) {
        pad = (unsigned)b;
        for (long i = 0; i < a; i++) p_retro_run();
        puts("ok");
      } else if (!strcmp(cmd, "peek") && sscanf(line, "%*s %li %li", &a, &b) == 2) {
        for (long i = 0; i < b; i++) printf("%02x", (a + i) < (long)ramsz ? ram[a + i] : 0);
        putchar('\n');
      } else if (!strcmp(cmd, "poke") && sscanf(line, "%*s %li %2047s", &a, arg) == 2) {
        for (long i = 0; arg[2 * i] && arg[2 * i + 1]; i++) { unsigned v; sscanf(arg + 2 * i, "%2x", &v); if (a + i < (long)ramsz) ram[a + i] = v; }
        puts("ok");
      } else if (!strcmp(cmd, "shot") && sscanf(line, "%*s %2047s", arg) == 1) {
        dump(arg); puts("ok");
      } else if (!strcmp(cmd, "wav") && sscanf(line, "%*s %2047s", arg) == 1) {
        if (!strcmp(arg, "-")) wav_close(); else wav_open(arg, 48000);
        puts("ok");
      } else if (!strcmp(cmd, "quit")) break;
      else puts("?");
    }
    wav_close();
    p_retro_unload_game(); p_retro_deinit();
    return 0;
  }
  for (int i = 1; i <= frames; i++) {
    p_retro_run();
    if (i % every == 0) { char p[512]; snprintf(p, sizeof p, "%s%05d.ppm", argv[5], i); dump(p); }
  }
  { void *ram = p_retro_get_memory_data(RETRO_MEMORY_SYSTEM_RAM); size_t sz = p_retro_get_memory_size(RETRO_MEMORY_SYSTEM_RAM);
    char p[512]; snprintf(p, sizeof p, "%sram.bin", argv[5]);
    if (ram && sz) { FILE *o = fopen(p, "wb"); fwrite(ram, 1, sz, o); fclose(o); printf("system RAM %zu bytes dumped\n", sz); }
    else printf("core exposes no system RAM\n"); }
  printf("frames %d, last %ux%u, audio samples %ld (non-silent %ld)\n", frames, fw, fh, audio_frames, audio_nonzero);
  p_retro_unload_game(); p_retro_deinit();
  return 0;
}
