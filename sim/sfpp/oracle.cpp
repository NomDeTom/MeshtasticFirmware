// Vector generator for cross-checking the Python port against the firmware's own PinSketch.
//
// Compiles src/modules/Native/PinSketch.cpp directly - that file has no dependencies beyond the
// standard library, so the oracle is the shipping code rather than a copy of it. check_oracle.py
// runs the same cases through pinsketch.py and diffs the output.
//
//   g++ -O2 -I../../src/modules/Native -o oracle oracle.cpp ../../src/modules/Native/PinSketch.cpp
//
// Protocol: one command per line on stdin.
//   mul <a> <b>          -> field product
//   inv <a>              -> field inverse
//   sketch <cap> <e...>  -> serialized sketch, lowercase hex
//   decode <cap> <e...>  -> "fail", or the decoded members ascending
//   diff <cap> <n> <e...> -> sketch of the first n elements XOR the rest, then decode

#include "PinSketch.h"

#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

static void printSketch(const pinsketch::Sketch &s)
{
    std::vector<uint8_t> buf(s.serializedSize());
    s.serialize(buf.data());
    for (uint8_t b : buf)
        printf("%02x", b);
    printf("\n");
}

static void printDecode(const pinsketch::Sketch &s)
{
    std::vector<pinsketch::Element> out;
    if (!s.decode(out)) {
        printf("fail\n");
        return;
    }
    for (size_t i = 0; i < out.size(); i++)
        printf("%s%u", i ? " " : "", out[i]);
    printf("\n");
}

int main()
{
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        if (!(in >> cmd))
            continue;

        if (cmd == "mul") {
            uint32_t a, b;
            in >> a >> b;
            printf("%u\n", pinsketch::mul(a, b));
        } else if (cmd == "inv") {
            uint32_t a;
            in >> a;
            printf("%u\n", pinsketch::inv(a));
        } else if (cmd == "sketch" || cmd == "decode") {
            size_t cap;
            in >> cap;
            pinsketch::Sketch s(cap);
            uint32_t e;
            while (in >> e)
                s.add(e);
            if (cmd == "sketch")
                printSketch(s);
            else
                printDecode(s);
        } else if (cmd == "diff") {
            size_t cap, n;
            in >> cap >> n;
            pinsketch::Sketch a(cap), b(cap);
            uint32_t e;
            size_t i = 0;
            while (in >> e)
                (i++ < n ? a : b).add(e);
            a.merge(b);
            printDecode(a);
        } else {
            printf("?\n");
        }
        fflush(stdout);
    }
    return 0;
}
