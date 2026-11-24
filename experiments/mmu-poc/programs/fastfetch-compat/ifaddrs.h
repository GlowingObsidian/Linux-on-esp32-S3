/* SPDX-License-Identifier: MIT
 * IPv4 interface enumeration for the Nano's IPv4-only Linux configuration.
 * Standard getifaddrs ABI; addresses and flags come from kernel ioctls.
 */
#ifndef FASTFETCH_XTENSA_IFADDRS_H
#define FASTFETCH_XTENSA_IFADDRS_H
#include <sys/socket.h>
struct ifaddrs {
    struct ifaddrs *ifa_next;
    char *ifa_name;
    unsigned int ifa_flags;
    struct sockaddr *ifa_addr;
    struct sockaddr *ifa_netmask;
    union { struct sockaddr *ifu_broadaddr; struct sockaddr *ifu_dstaddr; } ifa_ifu;
    void *ifa_data;
};
#define ifa_broadaddr ifa_ifu.ifu_broadaddr
#define ifa_dstaddr ifa_ifu.ifu_dstaddr
int getifaddrs(struct ifaddrs **result);
void freeifaddrs(struct ifaddrs *addresses);
#endif
