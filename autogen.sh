#!/bin/sh
# Run this to generate all the initial makefiles, etc.

test -n "$srcdir" || srcdir=`dirname "$0"`
test -n "$srcdir" || srcdir=.

if [ "$#" = 0 ] && [ -z "$NOCONFIGURE" ]; then
    echo "*** WARNING: I am going to run \`configure' with no arguments." >&2
    echo "*** If you wish to pass any to it, please specify them on the" >&2
    echo "*** \`$0' command line." >&2
    echo "" >&2
fi

( cd "$srcdir" && autoreconf -fiv && rm -rf autom4te.cache )

if [ -z "$NOCONFIGURE" ]; then
    "$srcdir/configure" "$@" || exit 1
fi
