#include <stdint.h>
__attribute__((section(".text"), used))
int stageVerifyStub(uint64_t arg0, uint32_t* arg1, uint64_t* arg2) {
    uint8_t* isEncrypted = (uint8_t*)0x02020299;
    uint32_t* cryptMemSize = (uint32_t*)0x02020290;
    uint32_t memSize = arg1[0x40c/4];
    arg2[0] = memSize;
    isEncrypted[0] = 1;
    cryptMemSize[0] = memSize;
    return (memSize > 0);
}
