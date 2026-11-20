/* C parser integration fixture: structs, functions, globals, calls. */

#include <stdio.h>
#include <stdlib.h>

int g_count = 0;
const int MAX_ITEMS = 64;

struct Point {
    int x;
    int y;
};

/* anonymous aggregate introduced via typedef (very common in C) */
typedef struct {
    double lat;
    double lng;
} Coord;

/* function-pointer variable: must be a variable, not a function */
int (*g_comparator)(int, int);

/* function prototype (declaration only) */
int add(int a, int b);

int add(int a, int b) {
    return a + b;
}

int compute(int base) {
    int result = add(base, MAX_ITEMS);
    return result;
}

void run(void) {
    int total = compute(g_count);
    printf("%d\n", total);
}
