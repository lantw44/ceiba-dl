#                                               -*- Autoconf -*-
# Process this file with autoconf to produce a configure script.

AC_INIT([ceiba-dl], [0.7], [https://github.com/lantw44/ceiba-dl/issues])
AC_CONFIG_SRCDIR([ceiba_dl/__init__.py])
AC_CONFIG_HEADERS([config.h])

AM_INIT_AUTOMAKE([foreign subdir-objects])
AM_SILENT_RULES([yes])

AC_ARG_ENABLE([helper-webkitgtk],
    [AC_HELP_STRING([--disable-helper-webkitgtk],
        [Disable WebKitGTK-based login helper])],
    [enable_helper_webkitgtk="$enableval"],
    [enable_helper_webkitgtk="yes"])
AM_CONDITIONAL([ENABLE_HELPER_WEBKITGTK], [test "$enable_helper_webkitgtk" = "yes"])

AC_PROG_CC
AC_PROG_CC_STDC
AM_PATH_PYTHON([3])
case "$PYTHON" in
    /*)
        ;;
    *)
        AC_MSG_ERROR([The path to python interpreter must be absolute])
        ;;
esac
AX_PYTHON_MODULE([lxml], [1])
AX_PYTHON_MODULE([pycurl], [1])
AX_PYTHON_MODULE([xdg], [1])

ceiba_dl_pythondir='${pythondir}/ceiba_dl'
AC_SUBST([ceiba_dl_pythondir], [${ceiba_dl_pythondir}])

if test "$enable_helper_webkitgtk" = "yes"; then
    AX_IS_RELEASE([git-directory])
    AX_COMPILER_FLAGS()
    case "$WARN_CFLAGS" in
        *-Wdeclaration-after-statement*)
            WARN_CFLAGS="$WARN_CFLAGS -Wno-declaration-after-statement"
            ;;
    esac
    PKG_CHECK_MODULES([WEBKITGTK], [webkit2gtk-4.0 >= 2.20], [],
        [AC_MSG_ERROR([

   WebKitGTK library cannot be found (Filename: webkit2gtk-4.0.pc)

   If you don't want to use the WebKitGTK-based login helper program,
   please pass --disable-helper-webkitgtk to the configure script.

        ])])
    AC_FUNC_FORK
    if test "$ac_cv_func_fork_works" '!=' "yes"; then
        AC_MSG_ERROR([WebKitGTK-based login helper requires a working fork function])
    fi
fi

AC_CONFIG_FILES([Makefile])
AC_OUTPUT
