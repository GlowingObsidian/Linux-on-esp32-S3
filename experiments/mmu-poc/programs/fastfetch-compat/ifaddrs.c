/* SPDX-License-Identifier: MIT */
#include "ifaddrs.h"
#include <errno.h>
#include <net/if.h>
#include <netpacket/packet.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

struct entry {
    struct ifaddrs address;
    char name[IFNAMSIZ];
    struct sockaddr_storage value, mask, broadcast;
};

void freeifaddrs(struct ifaddrs *address)
{
    while (address) {
        struct ifaddrs *next = address->ifa_next;
        free(address);
        address = next;
    }
}

int getifaddrs(struct ifaddrs **result)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct ifconf config;
    char *buffer = NULL;
    size_t size = 1024;
    struct ifaddrs **tail = result;
    *result = NULL;
    if (fd < 0) return -1;
    for (;;) {
        char *next = realloc(buffer, size);
        if (!next) goto failure;
        buffer = next;
        config.ifc_len = (int)size;
        config.ifc_buf = buffer;
        if (ioctl(fd, SIOCGIFCONF, &config) < 0) goto failure;
        if ((size_t)config.ifc_len + sizeof(struct ifreq) < size) break;
        if (size >= 65536) { errno = EOVERFLOW; goto failure; }
        size *= 2;
    }
    for (int offset = 0; offset + (int)sizeof(struct ifreq) <= config.ifc_len;
         offset += (int)sizeof(struct ifreq)) {
        struct ifreq request = *(struct ifreq *)(buffer + offset);
        struct entry *entry = calloc(1, sizeof(*entry));
        if (!entry) goto failure;
        *tail = &entry->address;
        tail = &entry->address.ifa_next;
        memcpy(entry->name, request.ifr_name, IFNAMSIZ);
        entry->name[IFNAMSIZ - 1] = 0;
        entry->address.ifa_name = entry->name;
        memcpy(&entry->value, &request.ifr_addr, sizeof(request.ifr_addr));
        entry->address.ifa_addr = (struct sockaddr *)&entry->value;
        if (ioctl(fd, SIOCGIFFLAGS, &request) < 0) goto failure;
        entry->address.ifa_flags = (unsigned short)request.ifr_flags;
        if (ioctl(fd, SIOCGIFNETMASK, &request) == 0) {
            memcpy(&entry->mask, &request.ifr_netmask, sizeof(request.ifr_netmask));
            entry->address.ifa_netmask = (struct sockaddr *)&entry->mask;
        }
        if (ioctl(fd, SIOCGIFBRDADDR, &request) == 0) {
            memcpy(&entry->broadcast, &request.ifr_broadaddr, sizeof(request.ifr_broadaddr));
            entry->address.ifa_broadaddr = (struct sockaddr *)&entry->broadcast;
        }
        if (ioctl(fd, SIOCGIFHWADDR, &request) == 0) {
            struct entry *mac = calloc(1, sizeof(*mac));
            if (!mac) goto failure;
            *tail = &mac->address;
            tail = &mac->address.ifa_next;
            memcpy(mac->name, entry->name, IFNAMSIZ);
            mac->address.ifa_name = mac->name;
            mac->address.ifa_flags = entry->address.ifa_flags;
            mac->address.ifa_addr = (struct sockaddr *)&mac->value;
            struct sockaddr_ll *link = (struct sockaddr_ll *)&mac->value;
            link->sll_family = AF_PACKET;
            link->sll_ifindex = (int)if_nametoindex(mac->name);
            link->sll_hatype = request.ifr_hwaddr.sa_family;
            link->sll_halen = 6;
            memcpy(link->sll_addr, request.ifr_hwaddr.sa_data, 6);
        }
    }
    free(buffer);
    close(fd);
    return 0;
failure: {
    int saved = errno;
    free(buffer);
    close(fd);
    freeifaddrs(*result);
    *result = NULL;
    errno = saved;
    return -1;
}}
