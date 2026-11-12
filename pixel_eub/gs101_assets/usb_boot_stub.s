
.text
.global _start
_start:
    // 1. Restore USB handler pointer at 0x02021990
    mov     x0, #0x1990
    movk    x0, #0x0202, lsl #16
    mov     w1, #0xa340
    str     w1, [x0]

    // 2. Set 0x02021a00 to 0xc4f8
    mov     x0, #0x1a00
    movk    x0, #0x0202, lsl #16
    mov     w1, #0xc4f8
    str     x1, [x0]

    // 3. Point stage check callback at 0x020200b0 to stageVerifyStub (0x02022600)
    mov     x0, #0x00b0
    movk    x0, #0x0202, lsl #16
    mov     w1, #0x2600
    movk    w1, #0x0202, lsl #16
    str     w1, [x0]

    // 4. Set USB boot flags for BootROM
    mov     x0, #0x0290
    movk    x0, #0x0202, lsl #16
    mov     w1, #1
    strb    w1, [x0, #0xd]      // DAT_0202029d = 1
    strb    w1, [x0, #0xa]      // DAT_0202029a = 1

    // 5. Set stack return address to 0x5664
    mov     x0, #0x13d8
    movk    x0, #0x0202, lsl #16
    mov     w1, #0x5664
    str     x1, [x0]

    ret
