# vim: set sw=8 ts=8 sts=8 noet:

NULL =

EXTRA_DIST = \
	COPYING				\
	COPYING.GPL			\
	README.asciidoc			\
	ceiba-dl.py			\
	ceiba_dl/_version.py.in		\
	$(NULL)

CLEANFILES = \
	ceiba-dl			\
	ceiba_dl/_version.py		\
	$(NULL)

bin_SCRIPTS = ceiba-dl

ceiba_dl_python_PYTHON = \
	ceiba_dl/__init__.py		\
	ceiba_dl/config.py		\
	ceiba_dl/helper.py		\
	ceiba_dl/vfs.py			\
	ceiba_dl/_version.py		\
	$(NULL)

helperdir = $(pkglibdir)/helpers

subst_cmd = sed \
	-e 's,[@]VERSION[@],$(VERSION),g'	\
	-e 's,[@]PYTHON[@],$(PYTHON),g'		\
	-e 's,[@]prefix[@],$(prefix),g'		\
	-e 's,[@]bindir[@],$(bindir),g'		\
	-e 's,[@]pkgdatadir[@],$(pkgdatadir),g'	\
	-e 's,[@]pythondir[@],$(pythondir),g'	\
	-e 's,[@]helperdir[@],$(helperdir),g'	\
	$(NULL)

chmod_verbose = $(chmod_verbose_@AM_V@)
chmod_verbose_ = $(chmod_verbose_@AM_DEFAULT_V@)
chmod_verbose_0 = @echo "  CHMOD   " $@;

mkdir_verbose = $(mkdir_verbose_@AM_V@)
mkdir_verbose_ = $(mkdir_verbose_@AM_DEFAULT_V@)
mkdir_verbose_0 = @echo "  MKDIR   " $@;

ceiba-dl: ceiba-dl.py Makefile
	$(AM_V_GEN)$(subst_cmd) $(srcdir)/ceiba-dl.py > ceiba-dl
	$(chmod_verbose)chmod +x ceiba-dl

ceiba_dl/_version.py: ceiba_dl/_version.py.in Makefile
	$(mkdir_verbose)$(MKDIR_P) ceiba_dl
	$(AM_V_GEN)$(subst_cmd) \
		$(srcdir)/ceiba_dl/_version.py.in > ceiba_dl/_version.py

if ENABLE_HELPER_WEBKITGTK

helper_PROGRAMS = helpers/10-helper-webkitgtk
helpers_10_helper_webkitgtk_SOURCES = helpers/helper-webkitgtk.c
helpers_10_helper_webkitgtk_CFLAGS = $(WARN_CFLAGS)
helpers_10_helper_webkitgtk_CPPFLAGS = $(WEBKITGTK_CFLAGS)
helpers_10_helper_webkitgtk_LDADD = $(WEBKITGTK_LIBS)

endif
