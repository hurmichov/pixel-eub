#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <libusb-1.0/libusb.h>
#include <ctype.h>
#include <sys/time.h>
#include <stdarg.h>
#include <time.h>
#include <sys/stat.h>

#define VENDOR_ID    0x18d1
#define PRODUCT_ID   0x4f00   /* BootROM / EPBL mode */
#define FASTBOOT_PID 0x4ee0   /* Fastboot mode */
#define CTRL_INTF 0
#define DATA_INTF 1
#define EP_OUT 0x02
#define EP_IN  0x81

#define USB_STUB_ENTRY 0x02024900  /* USB boot stub at BL1 offset 0x2900 -> IRAM */
#define INFLATE_BYTES  512         /* DNW frame padding to keep BootROM waiting */
#define CHUNK_SIZE     4096        /* USB bulk transfer chunk */

static FILE *g_log_file = NULL;
static FILE *g_log_file_dated = NULL;
static struct timespec g_t0;
static int g_t0_init = 0;
static int g_at_line_start = 1;

static double elapsed_sec(void) {
    if (!g_t0_init) {
        clock_gettime(CLOCK_MONOTONIC, &g_t0);
        g_t0_init = 1;
        return 0.0;
    }
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (now.tv_sec - g_t0.tv_sec) + (now.tv_nsec - g_t0.tv_nsec) / 1e9;
}

static long elapsed_ms_since(const struct timespec *start) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (now.tv_sec - start->tv_sec) * 1000L + (now.tv_nsec - start->tv_nsec) / 1000000L;
}

static void close_logs(void) {
    if (g_log_file) { fflush(g_log_file); fclose(g_log_file); g_log_file = NULL; }
    if (g_log_file_dated) { fflush(g_log_file_dated); fclose(g_log_file_dated); g_log_file_dated = NULL; }
}

static void log_msg(const char *fmt, ...) {
    char buf[2048];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    if (n <= 0) return;
    size_t len = (size_t)n < sizeof(buf) ? (size_t)n : sizeof(buf) - 1;

    char ts[24];
    int ts_len = snprintf(ts, sizeof(ts), "[+%6.2fs] ", elapsed_sec());
    int need_prefix = g_at_line_start;

    if (need_prefix && buf[0] != '\n') {
        fwrite(ts, 1, ts_len, stdout);
        if (g_log_file) fwrite(ts, 1, ts_len, g_log_file);
        if (g_log_file_dated) fwrite(ts, 1, ts_len, g_log_file_dated);
    }

    fwrite(buf, 1, len, stdout);
    if (g_log_file) { fwrite(buf, 1, len, g_log_file); fflush(g_log_file); }
    if (g_log_file_dated) { fwrite(buf, 1, len, g_log_file_dated); fflush(g_log_file_dated); }

    g_at_line_start = (buf[len - 1] == '\n');
}
#define printf(...) log_msg(__VA_ARGS__)

static void setup_usb(libusb_device_handle *h) {
    if (!h) return;
    for (int i = 0; i <= 1; i++) {
        if (libusb_kernel_driver_active(h, i) == 1)
            libusb_detach_kernel_driver(h, i);
        libusb_claim_interface(h, i);
    }
    uint8_t lc[7] = {0x00, 0xC2, 0x01, 0x00, 0x00, 0x00, 0x08};
    libusb_control_transfer(h, 0x21, 0x20, 0, CTRL_INTF, lc, 7, 500);
    libusb_control_transfer(h, 0x21, 0x22, 0x0003, CTRL_INTF, NULL, 0, 500);
}

static uint8_t *read_file(const char *path, size_t *out_size) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0) { fclose(f); return NULL; }
    uint8_t *buf = malloc((size_t)sz);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    *out_size = got;
    return buf;
}

static uint8_t *read_stage_file(const char *dev_dir, const char *rel_path, size_t *out_size, int required) {
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s", dev_dir, rel_path);
    uint8_t *buf = read_file(path, out_size);
    if (!buf && required) {
        printf("[-] FATAL: Cannot load required file: %s\n", path);
    }
    return buf;
}

static int check_fastboot(void) {
    libusb_device_handle *fb = libusb_open_device_with_vid_pid(NULL, VENDOR_ID, FASTBOOT_PID);
    if (fb) {
        libusb_close(fb);
        return 1;
    }
    return 0;
}

static int send_dnw(libusb_device_handle *h, const uint8_t *data, size_t size,
                    int inflate, const char *name) {
    if (!h) return -1;
    uint32_t total = 4 + 4 + (uint32_t)size + 2 + (uint32_t)inflate;
    uint32_t actual_frame_size = 4 + 4 + (uint32_t)size + 2;
    uint8_t *frame = calloc(1, actual_frame_size);
    if (!frame) return -1;

    frame[0] = 0x1B; frame[1] = 'D'; frame[2] = 'N'; frame[3] = 'W';
    memcpy(frame + 4, &total, 4);
    memcpy(frame + 8, data, size);

    uint16_t csum = 0;
    for (size_t i = 0; i < size; i++) csum += data[i];
    frame[8 + size]     = csum & 0xFF;
    frame[8 + size + 1] = (csum >> 8) & 0xFF;

    printf("[*] Sending %s (%zu bytes, frame %u bytes, inflate %d)...\n",
           name, size, actual_frame_size, inflate);

    uint8_t *ptr = frame;
    uint32_t rem = actual_frame_size;
    int sent_total = 0;
    while (rem > 0) {
        int chunk = rem < CHUNK_SIZE ? (int)rem : CHUNK_SIZE;
        int transferred = 0;
        int r = libusb_bulk_transfer(h, EP_OUT, ptr, chunk, &transferred, 8000);
        sent_total += transferred;
        if (r < 0) {
            printf("[-] Send failed on %s after %d bytes: %s (xfer=%d)\n",
                   name, sent_total, libusb_error_name(r), transferred);
            libusb_clear_halt(h, EP_OUT);
            free(frame);
            return -1;
        }
        ptr += transferred;
        rem -= (uint32_t)transferred;
        if (rem > 0) usleep(2000);
    }
    printf("[+] %s sent (%d bytes)!\n", name, sent_total);
    free(frame);
    return 0;
}

static void parse_eub_req(const char *msg, char *req_out, size_t req_out_sz) {
    const char *p = strstr(msg, "eub:req:");
    while (p) {
        const char *c1 = strchr(p + 8, ':');
        if (c1) {
            char tmp[32] = {0};
            size_t k = 0;
            c1++;
            while (*c1 && !isspace((unsigned char)*c1) && *c1 != 'C' && k < sizeof(tmp) - 1) {
                tmp[k++] = (char)toupper((unsigned char)*c1);
                c1++;
            }
            if (k > 0) {
                snprintf(req_out, req_out_sz, "%s", tmp);
            }
        }
        p = strstr(p + 8, "eub:req:");
    }
}

static int wait_for_c_handshake(libusb_device_handle *h, int timeout_ms,
                                const char *stage_name, char *last_req, size_t last_req_sz) {
    if (!h) return 0;
    struct timespec t_start;
    clock_gettime(CLOCK_MONOTONIC, &t_start);
    printf("[*] Waiting for 'C' handshake for [%s] (up to %d ms)...\n", stage_name, timeout_ms);

    while (elapsed_ms_since(&t_start) < timeout_ms) {
        if (check_fastboot()) return 2;

        uint8_t rx[512] = {0};
        int xfer = 0;
        int r = libusb_bulk_transfer(h, EP_IN, rx, sizeof(rx) - 1, &xfer, 100);

        if (r == 0 && xfer > 0) {
            rx[xfer] = 0;
            printf("  < Device: %s\n", rx);
            if (last_req && last_req_sz > 0)
                parse_eub_req((const char *)rx, last_req, last_req_sz);

            for (int i = 0; i < xfer; i++) {
                if (rx[i] == 'C') {
                    printf("[+] Handshake 'C' confirmed! Device buffer ready for %s.\n", stage_name);
                    return 1;
                }
            }
        } else if (r == LIBUSB_ERROR_NO_DEVICE) {
            printf("[-] Device disconnected while waiting for 'C' on [%s]!\n", stage_name);
            return -1;
        } else if (r == LIBUSB_ERROR_PIPE || r == LIBUSB_ERROR_IO) {
            libusb_clear_halt(h, EP_IN);
            usleep(20000);
        }
    }

    printf("[-] Timeout waiting for 'C' on [%s] (%ld ms elapsed)!\n",
           stage_name, elapsed_ms_since(&t_start));
    return 0;
}

static int deliver_stage(libusb_device_handle *h, const char *stage_name,
                         const uint8_t *data, size_t size, int delay_after_ms) {
    printf("\n------------------------------------------------------------\n");
    printf("[*] Preparing stage: %s (%zu bytes)\n", stage_name, size);

    char last_req[32] = {0};
    int c_res = wait_for_c_handshake(h, 25000, stage_name, last_req, sizeof(last_req));
    if (c_res == 2) return 1;
    if (c_res <= 0) {
        printf("[-] Phone did not send 'C' for %s within 25s!\n", stage_name);
        return -1;
    }

    if (last_req[0]) {
        printf("[*] BootROM requested tag: [%s] -> sending [%s]\n", last_req, stage_name);
    }

    usleep(10000);
    if (send_dnw(h, data, size, 0, stage_name) < 0) {
        printf("[-] FAILED to deliver stage %s!\n", stage_name);
        return -1;
    }

    printf("[+] %s delivered successfully!\n", stage_name);
    if (delay_after_ms > 0) usleep((useconds_t)delay_after_ms * 1000);
    return 0;
}

typedef struct {
    const char *tag;
    const uint8_t *data;
    size_t size;
    const char *label;
} stage_entry_t;

static const stage_entry_t *find_stage(const stage_entry_t *stages, int n, const char *tag) {
    for (int i = 0; i < n; i++) {
        if (strcmp(stages[i].tag, tag) == 0) return &stages[i];
    }
    return NULL;
}

static int serve_epbl_stages(libusb_device_handle **h_ptr, const stage_entry_t *stages, int n_stages) {
    char pending_tag[32] = {0};
    int seen_c_for_pending = 0;
    int stages_served = 0;
    struct timespec last_activity;
    clock_gettime(CLOCK_MONOTONIC, &last_activity);

    printf("\n================================================================\n");
    printf("   >>> REACTIVE EPBL STAGE SERVER ACTIVE (reading eub:req:) <<<\n");
    printf("================================================================\n");
    fflush(stdout);

    while (1) {
        if (check_fastboot()) return 1;

        long idle_ms = elapsed_ms_since(&last_activity);
        if (idle_ms > 30000) {
            printf("[-] Reactive server idle timeout (%ld ms, %d stages served).\n",
                   idle_ms, stages_served);
            return 0;
        }

        if (!*h_ptr) {
            *h_ptr = libusb_open_device_with_vid_pid(NULL, VENDOR_ID, PRODUCT_ID);
            if (*h_ptr) {
                printf("[+] Reconnected to 18d1:4f00!\n");
                setup_usb(*h_ptr);
                clock_gettime(CLOCK_MONOTONIC, &last_activity);
            } else {
                usleep(50000);
                continue;
            }
        }

        uint8_t rx[512] = {0};
        int xfer = 0;
        int r = libusb_bulk_transfer(*h_ptr, EP_IN, rx, sizeof(rx) - 1, &xfer, 100);

        if (r == 0 && xfer > 0) {
            rx[xfer] = 0;
            clock_gettime(CLOCK_MONOTONIC, &last_activity);
            printf("  < Device: %s\n", rx);

            const char *req_pos = strstr((const char *)rx, "eub:req:");
            if (req_pos) {
                parse_eub_req((const char *)rx, pending_tag, sizeof(pending_tag));
                seen_c_for_pending = 0;
                const char *after = strrchr(req_pos, ':');
                if (after) {
                    const char *tag_end = after + 1;
                    while (*tag_end && !isspace((unsigned char)*tag_end) && *tag_end != 'C') tag_end++;
                    for (const char *q = tag_end; q < (const char *)rx + xfer; q++) {
                        if (*q == 'C') { seen_c_for_pending = 1; break; }
                    }
                }
            } else {
                for (int i = 0; i < xfer; i++) {
                    if (rx[i] == 'C') { seen_c_for_pending = 1; break; }
                }
            }

            if (pending_tag[0] && seen_c_for_pending) {
                const stage_entry_t *st = find_stage(stages, n_stages, pending_tag);
                if (!st || !st->data || st->size == 0) {
                    printf("[-] ERROR: Device requested stage [%s], which is not loaded!\n", pending_tag);
                    return -1;
                }
                printf("[*] Serving [%s] -> %s (%zu bytes)\n", pending_tag, st->label, st->size);
                usleep(5000);
                if (send_dnw(*h_ptr, st->data, st->size, 0, st->label) < 0) {
                    printf("[-] Failed to send [%s]!\n", pending_tag);
                    return -1;
                }
                stages_served++;
                int sent_abl_or_bl31 = (strcmp(pending_tag, "ABLB") == 0 ||
                                        strcmp(pending_tag, "BL3B") == 0 ||
                                        strcmp(pending_tag, "BL31B") == 0);
                pending_tag[0] = 0;
                seen_c_for_pending = 0;
                clock_gettime(CLOCK_MONOTONIC, &last_activity);
                if (sent_abl_or_bl31) {
                    printf("[*] Key stage delivered (total=%d), continuing to listen / check Fastboot...\n",
                           stages_served);
                }
            }
        } else if (r == LIBUSB_ERROR_NO_DEVICE) {
            printf("[*] USB 18d1:4f00 disconnected (%d stages served) — checking Fastboot...\n",
                   stages_served);
            libusb_release_interface(*h_ptr, 0);
            libusb_release_interface(*h_ptr, 1);
            libusb_close(*h_ptr);
            *h_ptr = NULL;
            for (int w = 0; w < 400; w++) {
                if (check_fastboot()) return 1;
                libusb_device_handle *probe = libusb_open_device_with_vid_pid(NULL, VENDOR_ID, PRODUCT_ID);
                if (probe) {
                    *h_ptr = probe;
                    setup_usb(*h_ptr);
                    printf("[*] 18d1:4f00 reappeared!\n");
                    clock_gettime(CLOCK_MONOTONIC, &last_activity);
                    break;
                }
                usleep(100000);
            }
            if (!*h_ptr) return 0;
        } else if (r == LIBUSB_ERROR_PIPE || r == LIBUSB_ERROR_IO) {
            libusb_clear_halt(*h_ptr, EP_IN);
            usleep(20000);
        }
    }
}

static int execute_bl1_usb_init(libusb_device_handle *h, const uint8_t *bl1_stage, size_t bl1_size) {
    printf("[*] Sending BL1+USBStub (%zu bytes, frame %zu bytes, pad %d)...\n",
           bl1_size, bl1_size + 10, INFLATE_BYTES);
    if (send_dnw(h, bl1_stage, bl1_size, INFLATE_BYTES, "BL1+USBStub") < 0) {
        printf("[-] Failed to send BL1 stage!\n");
        return -1;
    }

    usleep(25000);

    printf("\n[*] Initializing USB BootROM recovery session...\n");
    uint8_t work[0x494];
    memset(work, 0, sizeof(work));
    int r = libusb_control_transfer(h, 0x80, 0x08, 0, 0, work, 0x494, 2000);
    if (r < 0) {
        printf("[-] IRAM status read failed: %s\n", libusb_error_name(r));
        return -1;
    }
    printf("[+] Read %d bytes from IRAM USB state\n", r);

    uint32_t event_counter = *(uint32_t *)&work[0x480];
    uint32_t handler_ptr   = *(uint32_t *)&work[0x490];
    printf("[+] event_counter = %u, handler_ptr = 0x%08x\n", event_counter, handler_ptr);

    *(uint32_t *)&work[0x480] = event_counter + 5;
    *(uint32_t *)&work[0x490] = USB_STUB_ENTRY;

    printf("[*] Updating USB callback handler -> 0x%08x (USB boot stub)...\n", USB_STUB_ENTRY);
    r = libusb_control_transfer(h, 0x00, 0x08, 0, 0, work, 0x494, 1500);
    if (r != LIBUSB_ERROR_PIPE) {
        printf("[-] USB init handshake failed! (control_transfer: %d / %s — expected LIBUSB_ERROR_PIPE (-9))\n",
               r, libusb_error_name(r));
        return -1;
    }
    printf("[+] USB init handshake complete! (control_transfer: %d / %s — confirmed)\n",
           r, libusb_error_name(r));

    usleep(100000);

    uint8_t flush_pad[INFLATE_BYTES];
    memset(flush_pad, 0, sizeof(flush_pad));
    int flush_xfer = 0;
    int fr = libusb_bulk_transfer(h, EP_OUT, flush_pad, INFLATE_BYTES, &flush_xfer, 1500);
    printf("[*] Flush EP_OUT (%d bytes) to finish BootROM BL1 receive: r=%d (%s), xfer=%d\n",
           INFLATE_BYTES, fr, (fr == 0 ? "OK" : libusb_error_name(fr)), flush_xfer);
    if (fr < 0 && flush_xfer < INFLATE_BYTES) {
        libusb_clear_halt(h, EP_OUT);
        int rem = INFLATE_BYTES - flush_xfer;
        int retry_xfer = 0;
        int rr = libusb_bulk_transfer(h, EP_OUT, flush_pad + flush_xfer, rem, &retry_xfer, 1500);
        printf("[*] Flush retry (%d bytes): r=%d (%s), xfer=%d\n",
               rem, rr, (rr == 0 ? "OK" : libusb_error_name(rr)), retry_xfer);
    }

    printf("\n================================================================\n");
    printf("  >>> USB BOOT HANDLER ACTIVE! Stub executed at 0x%08x <<<\n", USB_STUB_ENTRY);
    printf("  >>> Waiting for BootROM ACK & 'C' -> DPM -> EPBL <<<\n");
    printf("================================================================\n");
    fflush(stdout);
    return 0;
}

static int file_exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0 && S_ISREG(st.st_mode);
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    char dev_dir[512] = "devices/bluejay";
    const char *device_label = "bluejay";

    if (argc >= 2) {
        if (strcmp(argv[1], "--dir") == 0 && argc >= 3) {
            snprintf(dev_dir, sizeof(dev_dir), "%s", argv[2]);
            device_label = argv[2];
        } else if (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0) {
            fprintf(stderr, "Usage: %s [oriole|raven|bluejay | --dir <prepared_dir>]\n", argv[0]);
            return 0;
        } else {
            device_label = argv[1];
            snprintf(dev_dir, sizeof(dev_dir), "devices/%s", argv[1]);
        }
    }

    mkdir("logs", 0777);
    time_t now = time(NULL);
    struct tm *t = localtime(&now);
    char dated_log[256];
    strftime(dated_log, sizeof(dated_log), "logs/gs101_revive_%Y%m%d_%H%M%S.log", t);

    g_log_file = fopen("logs/gs101_revive.log", "w");
    g_log_file_dated = fopen(dated_log, "w");
    atexit(close_logs);
    clock_gettime(CLOCK_MONOTONIC, &g_t0);
    g_t0_init = 1;

    libusb_init(NULL);

    printf("================================================================\n");
    printf("  Pixel 6 / 6 Pro / 6a (GS101) USB Recovery -> Fastboot\n");
    printf("  Target profile: %s (%s)\n", device_label, dev_dir);
    printf("================================================================\n");
    printf("[*] Logs saving to:\n");
    printf("    -> %s\n", dated_log);
    printf("    -> logs/gs101_revive.log\n");

    char check_path[1024];
    snprintf(check_path, sizeof(check_path), "%s/bl1_recovery.bin", dev_dir);
    if (!file_exists(check_path)) {
        printf("[-] FATAL: Prepared stage directory not found: %s\n", check_path);
        printf("[-] Run first: python3 scripts/gs101_prepare.py --device <oriole|raven|bluejay>\n");
        return 1;
    }

    size_t bl1_size = 0, epbl_size = 0;
    size_t bl2_hdr_size = 0, bl2b_size = 0;
    size_t bl31_hdr_size = 0, bl31b_size = 0;
    size_t abl_hdr_size = 0, ablb_size = 0;
    size_t tzsw_full_size = 0, ldfw_full_size = 0, gsa_full_size = 0;

    uint8_t *bl1_stage   = read_stage_file(dev_dir, "bl1_recovery.bin", &bl1_size, 1);
    uint8_t *epbl        = read_stage_file(dev_dir, "pbl_recovery.img", &epbl_size, 1);
    uint8_t *bl2_hdr     = read_stage_file(dev_dir, "split_stages/BL2.bin", &bl2_hdr_size, 1);
    uint8_t *bl2b        = read_stage_file(dev_dir, "BL2B_recovery.bin", &bl2b_size, 1);
    uint8_t *bl31_hdr    = read_stage_file(dev_dir, "split_stages/BL31.bin", &bl31_hdr_size, 1);
    uint8_t *bl31b       = read_stage_file(dev_dir, "split_stages/BL31B.bin", &bl31b_size, 1);
    uint8_t *abl_hdr     = read_stage_file(dev_dir, "split_stages/ABL.bin", &abl_hdr_size, 1);
    uint8_t *ablb        = read_stage_file(dev_dir, "ABLB_recovery.bin", &ablb_size, 1);
    uint8_t *tzsw_full   = read_stage_file(dev_dir, "partitions/tzsw.img", &tzsw_full_size, 1);
    uint8_t *ldfw_full   = read_stage_file(dev_dir, "partitions/ldfw.img", &ldfw_full_size, 1);
    uint8_t *gsa_full    = read_stage_file(dev_dir, "partitions/gsa.img", &gsa_full_size, 1);

    if (!bl1_stage || !epbl || !bl2_hdr || !bl2b || !bl31_hdr || !bl31b ||
        !abl_hdr || !ablb || !tzsw_full || !ldfw_full || !gsa_full) {
        return 1;
    }

    uint8_t *dpm = calloc(1, 4096);
    size_t dpm_size = 4096;

    const uint8_t *tzsw_hdr  = (tzsw_full_size > 4096) ? tzsw_full : NULL;
    size_t tzsw_hdr_size     = tzsw_hdr ? 4096 : 0;
    const uint8_t *tzsw_body = tzsw_hdr ? (tzsw_full + 4096) : NULL;
    size_t tzsw_body_size    = tzsw_hdr ? (tzsw_full_size - 4096) : 0;

    const uint8_t *ldfw_hdr  = (ldfw_full_size > 4096) ? ldfw_full : NULL;
    size_t ldfw_hdr_size     = ldfw_hdr ? 4096 : 0;
    const uint8_t *ldfw_body = ldfw_hdr ? (ldfw_full + 4096) : NULL;
    size_t ldfw_body_size    = ldfw_hdr ? (ldfw_full_size - 4096) : 0;

    const stage_entry_t stages[] = {
        {"BL2",   bl2_hdr,   bl2_hdr_size,   "BL2 (hdr)"},
        {"BL2B",  bl2b,      bl2b_size,      "BL2B (body)"},
        {"ABL",   abl_hdr,   abl_hdr_size,   "ABL (hdr)"},
        {"ABLB",  ablb,      ablb_size,      "ABLB (body)"},
        {"BL31",  bl31_hdr,  bl31_hdr_size,  "BL31 (hdr)"},
        {"BL3B",  bl31b,     bl31b_size,     "BL31B (body)"},
        {"BL31B", bl31b,     bl31b_size,     "BL31B (body)"},
        {"TZSW",  tzsw_hdr,  tzsw_hdr_size,  "TZSW (hdr)"},
        {"TZSB",  tzsw_body, tzsw_body_size, "TZSW (body)"},
        {"LDFW",  ldfw_hdr,  ldfw_hdr_size,  "LDFW (hdr)"},
        {"LDFB",  ldfw_body, ldfw_body_size, "LDFW (body)"},
        {"GSA1",  gsa_full,  gsa_full_size,  "GSA1 (full)"},
        {"DPM",   dpm,       dpm_size,       "DPM"},
        {"EPBL",  epbl,      epbl_size,      "EPBL"},
    };
    int n_stages = (int)(sizeof(stages) / sizeof(stages[0]));

    printf("[*] BootROM stages: BL1=%zu DPM=%zu EPBL=%zu\n", bl1_size, dpm_size, epbl_size);
    printf("[*] EPBL stages: BL2=%zu/%zu BL31=%zu/%zu ABL=%zu/%zu TZSW=%zu/%zu LDFW=%zu/%zu GSA1=%zu\n",
           bl2_hdr_size, bl2b_size, bl31_hdr_size, bl31b_size,
           abl_hdr_size, ablb_size, tzsw_hdr_size, tzsw_body_size,
           ldfw_hdr_size, ldfw_body_size, gsa_full_size);

    while (1) {
        if (check_fastboot()) goto fastboot_found;

        printf("\n[*] Waiting for Pixel (GS101) in BootROM mode (18d1:4f00)...\n");
        printf("[*] >>> FRESH CONNECTION: plug cable into PC, HOLD Power on phone, and insert cable! Repeat until detected! <<<\n");
        fflush(stdout);

        libusb_device_handle *h = NULL;
        while (!h) {
            if (check_fastboot()) goto fastboot_found;
            h = libusb_open_device_with_vid_pid(NULL, VENDOR_ID, PRODUCT_ID);
            if (!h) usleep(15000);
        }
        printf("[+] Device connected in BootROM mode!\n");
        setup_usb(h);

        char req_buf[32] = {0};
        int init_c = wait_for_c_handshake(h, 3000, "BL1 (initial)", req_buf, sizeof(req_buf));
        if (init_c == 2) goto fastboot_found;

        if (execute_bl1_usb_init(h, bl1_stage, bl1_size) < 0) {
            printf("[-] Failed executing BL1 USB init, retrying...\n");
            libusb_release_interface(h, 0);
            libusb_release_interface(h, 1);
            libusb_close(h);
            continue;
        }

        int res = deliver_stage(h, "DPM", dpm, dpm_size, 20);
        if (res == 1) goto fastboot_found;
        if (res < 0) {
            printf("[-] DPM delivery failed, restarting BootROM cycle...\n");
            libusb_release_interface(h, 0);
            libusb_release_interface(h, 1);
            libusb_close(h);
            continue;
        }

        res = deliver_stage(h, "EPBL", epbl, epbl_size, 50);
        if (res == 1) goto fastboot_found;
        if (res < 0) {
            printf("[-] EPBL delivery failed, restarting BootROM cycle...\n");
            libusb_release_interface(h, 0);
            libusb_release_interface(h, 1);
            libusb_close(h);
            continue;
        }

        res = serve_epbl_stages(&h, stages, n_stages);
        if (h) {
            libusb_release_interface(h, 0);
            libusb_release_interface(h, 1);
            libusb_close(h);
            h = NULL;
        }
        if (res == 1) goto fastboot_found;

        printf("\n[*] Polling Fastboot mode (18d1:4ee0) up to 40s...\n");
        fflush(stdout);
        for (int w = 0; w < 400; w++) {
            if (check_fastboot()) goto fastboot_found;
            usleep(100000);
        }
    }

fastboot_found:
    printf("\n============================================================\n");
    printf("   >>> FASTBOOT MODE ONLINE (18d1:4ee0)! <<<\n");
    printf("   >>> Fastboot USB detected; device identity and persistent recovery are not confirmed. <<<\n");
    printf("============================================================\n");

    free(bl1_stage);
    free(dpm);
    free(epbl);
    free(bl2_hdr);
    free(bl2b);
    free(bl31_hdr);
    free(bl31b);
    free(abl_hdr);
    free(ablb);
    free(tzsw_full);
    free(ldfw_full);
    free(gsa_full);

    libusb_exit(NULL);

    printf("\n[*] Running: fastboot devices\n");
    fflush(stdout);
    usleep(1500000);
    if (system("fastboot devices") != 0) {
        printf("[!] 'fastboot' command returned non-zero (check android-tools installation)\n");
    }
    return 0;
}
