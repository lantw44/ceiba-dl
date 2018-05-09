/*
 * NTU CEIBA login helper program - WebKitGTK+-based version
 * Copyright (C) 2017  Ting-Wei Lan
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Lesser General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 *
 */

#ifdef HAVE_CONFIG_H
# include "config.h"
#endif

#include <errno.h>
#include <locale.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <signal.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <glib.h>
#include <glib-object.h>
#include <gtk/gtk.h>
#include <libsoup/soup.h>
#include <webkit2/webkit2.h>

enum exit_status {
    EXIT_STATUS_PIPE_ERROR = 1,
    EXIT_STATUS_FORK_ERROR,
    EXIT_STATUS_DUP2_ERROR,
    EXIT_STATUS_STDIO_ERROR,
    EXIT_STATUS_GTK_INIT_ERROR,
    EXIT_STATUS_CLOSED_BY_USERS,
    EXIT_STATUS_STDIN_EARLY_EOF,
    EXIT_STATUS_STDIN_READ_ERROR,
};

typedef struct helper_data {
    WebKitWebView *web_view;
    char *login_uri;
    char *expected_uri;
    char *cookie_name;
    bool redirected;
    bool load_failed;
    GIOChannel *stdin_channel;
    bool stdin_input_accepted;
    FILE *output_fp;
} HelperData;

static void helper_data_init (HelperData *data) {
    data->web_view = NULL;
    data->login_uri = NULL;
    data->expected_uri = NULL;
    data->cookie_name = NULL;
    data->redirected = false;
    data->load_failed = false;
    data->stdin_channel = NULL;
    data->stdin_input_accepted = false;
    data->output_fp = NULL;
}

static void helper_data_fini (HelperData *data) {
    g_clear_object (&(data->web_view));
    g_clear_pointer (&(data->login_uri), g_free);
    g_clear_pointer (&(data->expected_uri), g_free);
    g_clear_pointer (&(data->cookie_name), g_free);
    g_clear_pointer (&(data->stdin_channel), g_io_channel_unref);
    g_clear_pointer (&(data->output_fp), fclose);
}

static inline void stdin_input_enable (HelperData *helper_data);

static void cookie_free (void *cookie) {
    soup_cookie_free (cookie);
}

static void cookies_ready_cb (GObject *cookie_manager_object,
    GAsyncResult *async_result, void *user_data) {

    HelperData *helper_data = user_data;
    GError *err = NULL;

    GList *cookies = webkit_cookie_manager_get_cookies_finish (
        WEBKIT_COOKIE_MANAGER (cookie_manager_object), async_result, &err);
    if (err != NULL) {
        g_warning ("無法取得 cookie：%s", err->message);
        g_error_free (err);
        fputc ('\n', helper_data->output_fp);
        goto free_cookies;
    }

    bool found = false;
    for (GList *l = cookies; l != NULL; l = l->next) {
        SoupCookie *cookie = l->data;
        if (strcmp (soup_cookie_get_name (cookie), helper_data->cookie_name) == 0) {
            fputs (soup_cookie_get_value (cookie), helper_data->output_fp);
            fputc ('\n', helper_data->output_fp);
            found = true;
            break;
        }
    }

    if (!found) {
        fputc ('\n', helper_data->output_fp);
    }

free_cookies:
    if (cookies != NULL) {
        g_list_free_full (cookies, cookie_free);
    }

    g_clear_pointer (&(helper_data->cookie_name), g_free);
    stdin_input_enable (helper_data);
}

static bool stdin_read (GIOChannel *stdin_channel,
    char **result, bool eof_allowed) {

    g_return_val_if_fail (result != NULL, FALSE);

    GError *err = NULL;
    size_t newline_offset;
    GIOStatus status = g_io_channel_read_line (
        stdin_channel, result, NULL, &newline_offset, &err);

    switch (status) {
        case G_IO_STATUS_NORMAL:
            g_debug ("標準輸入讀取狀態 - NORMAL");
            (*result)[newline_offset] = '\0';
            break;
        case G_IO_STATUS_EOF:
            g_debug ("標準輸入讀取狀態 - EOF");
            if (!eof_allowed) {
                g_critical ("無法從標準輸入讀取資料 - 輸入資料提前結束");
                exit (EXIT_STATUS_STDIN_EARLY_EOF);
            }
            break;
        case G_IO_STATUS_ERROR:
        case G_IO_STATUS_AGAIN:
            g_debug ("標準輸入讀取狀態 - ERROR 或 AGAIN");
            if (err != NULL) {
                g_critical ("無法從標準輸入讀取資料 - %s", err->message);
            } else {
                g_critical ("無法從標準輸入讀取資料");
            }
            exit (EXIT_STATUS_STDIN_READ_ERROR);
        default:
            g_assert_not_reached ();
    }

    return *result != NULL && (*result)[0] != '\0';
}

static gboolean stdin_ready_cb (GIOChannel *stdin_channel,
    GIOCondition condition, void *user_data) {

    HelperData *helper_data = user_data;
    g_return_val_if_fail (helper_data->cookie_name == NULL, FALSE);

    if (stdin_read (stdin_channel, &helper_data->cookie_name, true)) {
        webkit_cookie_manager_get_cookies (
            webkit_web_context_get_cookie_manager (
                webkit_web_view_get_context (helper_data->web_view)),
            helper_data->expected_uri, NULL, cookies_ready_cb, helper_data);
    } else {
        g_debug ("偵測到空白行或檔案結尾，準備結束");
        gtk_main_quit ();
    }

    return FALSE;
}

static inline void stdin_input_enable (HelperData *helper_data) {
    g_io_add_watch (helper_data->stdin_channel,
        G_IO_IN | G_IO_PRI, stdin_ready_cb, helper_data);
}

static gboolean web_view_decide_policy_cb (WebKitWebView *web_view,
    WebKitPolicyDecision *decision, WebKitPolicyDecisionType type, void *user_data) {

    switch (type) {
        case WEBKIT_POLICY_DECISION_TYPE_NAVIGATION_ACTION: {
                WebKitURIRequest *request =
                    webkit_navigation_action_get_request (
                        webkit_navigation_policy_decision_get_navigation_action (
                            WEBKIT_NAVIGATION_POLICY_DECISION (decision)));
                const char *uri = webkit_uri_request_get_uri (request);
                char *scheme = g_uri_parse_scheme (uri);
                if (g_strcmp0 (scheme, "https") != 0 &&
                    g_strcmp0 (scheme, "http") != 0) {
                    webkit_policy_decision_ignore (decision);
                } else {
                    webkit_policy_decision_use (decision);
                }
            } return TRUE;
        case WEBKIT_POLICY_DECISION_TYPE_NEW_WINDOW_ACTION:
            g_warning ("登入輔助程式不支援開啟新視窗");
            webkit_policy_decision_ignore (decision);
            return TRUE;
        case WEBKIT_POLICY_DECISION_TYPE_RESPONSE:
            return FALSE;
        default:
            return FALSE;
    }
    return FALSE;
}

static gboolean web_view_enter_fullscreen_cb (WebKitWebView *web_view, void* user_data) {
    // 我們應該不會遇到任何需要全螢幕的網頁
    return TRUE;
}

static void web_view_load_changed_cb (WebKitWebView *web_view,
    WebKitLoadEvent load_event, void *user_data) {

    HelperData *helper_data = user_data;
    switch (load_event) {
        case WEBKIT_LOAD_STARTED:
            helper_data->load_failed = false;
            break;
        case WEBKIT_LOAD_REDIRECTED:
            helper_data->redirected = true;
            break;
        case WEBKIT_LOAD_COMMITTED:
            break;
        case WEBKIT_LOAD_FINISHED: {
            const char *uri = webkit_web_view_get_uri (web_view);
            g_debug ("網頁載入結束 - 網址：%s", uri);

            if (helper_data->redirected &&
                !helper_data->load_failed &&
                !helper_data->stdin_input_accepted &&
                !g_str_has_prefix (uri, helper_data->login_uri) &&
                g_str_has_prefix (uri, helper_data->expected_uri)) {

                g_debug ("偵測到登入成功的網址，開始監測標準輸入");
                fputs ("OK\n", helper_data->output_fp);
                stdin_input_enable (helper_data);
                helper_data->stdin_input_accepted = true;
            }} break;
        default:
            g_assert_not_reached ();
    }
}

static gboolean web_view_load_failed_cb (WebKitWebView *web_view,
    WebKitLoadEvent load_event, char *failing_uri, GError *error, void *user_data) {

    HelperData *helper_data = user_data;
    helper_data->load_failed = true;
    g_warning ("網頁載入錯誤 - 網址：%s", webkit_web_view_get_uri (web_view));

    return FALSE;
}

static void web_view_update_load_progress_cb (WebKitWebView *web_view,
    GParamSpec *pspec, void *user_data) {

    GtkWindow *window = user_data;
    GObject *window_object = G_OBJECT (window);

    char *default_title = g_object_get_data (window_object, "default-title");
    double progress = webkit_web_view_get_estimated_load_progress (web_view);

    char *new_title = g_strdup_printf ("%s - %d%%",
        default_title, (int)(progress * 100.0));
    gtk_window_set_title (window, new_title);
    g_free (new_title);
}

static gboolean window_close_cb (GtkWidget *window,
    GdkEvent *event, void* data) {

    g_critical ("使用者關閉視窗 - 立即離開");
    exit (EXIT_STATUS_CLOSED_BY_USERS);
}

static int sigchld_write_fd;
static void sigchld_handler (int signo) {
    write (sigchld_write_fd, (char[]){ 1 }, 1);
}

int main (int argc, char *argv[]) {

    setlocale (LC_ALL, "");

    /* 由於 GLib 提供的 log 函式會把 INFO 和 DEBUG 級別的訊息送進 stdout，導致
     * 原有程式正常的輸出和偵錯用的訊息混合，使得讀取輔助程式的輸出的其他程式
     * 無法正確判讀資料。雖然 GLib 有提供變更輸出 log 用的函式的功能，但因為預
     * 設版本提供的功能複雜，不容易完全重新實作，所以在此我們把輔助程式拆成兩
     * 個程序：子程序負責顯示網頁、接收指令、輸出 cookie 值，父程序負責重導向
     * 子程序輸出，將正常的輸出放到 stdout，而 log 訊息全數轉到 stderr，
     */

    int stdout_pipe[2];
    int stderr_pipe[2];
    int output_pipe[2];
    int sigchld_pipe[2];

    if (pipe (stdout_pipe) || pipe (stderr_pipe) ||
        pipe (output_pipe) || pipe (sigchld_pipe)) {
        perror ("pipe");
        exit (EXIT_STATUS_PIPE_ERROR);
    }

    sigchld_write_fd = sigchld_pipe[1];

    struct sigaction act_chld, act_chld_old;
    act_chld.sa_handler = sigchld_handler;
    act_chld.sa_flags = SA_RESTART | SA_NOCLDSTOP;
    sigemptyset (&act_chld.sa_mask);
    sigaction (SIGCHLD, &act_chld, &act_chld_old);

    pid_t pid = fork ();
    if (pid < 0) {
        perror ("fork");
        exit (EXIT_STATUS_FORK_ERROR);
    }

    // 這段是父程序用來轉送子程序輸出的程式

    if (pid > 0) {
        close (stdout_pipe[1]);
        close (stderr_pipe[1]);
        close (output_pipe[1]);

        bool child_exited = false;
        int child_status = 0;

        while (!child_exited) {
            char buf[BUFSIZ];
            struct pollfd fds[4] = {
                { .fd = stdout_pipe[0], .events = POLLIN | POLLPRI },
                { .fd = stderr_pipe[0], .events = POLLIN | POLLPRI },
                { .fd = output_pipe[0], .events = POLLIN | POLLPRI },
                { .fd = sigchld_pipe[0], .events = POLLIN | POLLPRI }
            };

            if (poll (fds, 4, -1) < 0) {
                if (errno != EINTR) {
                    perror ("poll");
                }
                continue;
            }

            ssize_t read_count;
            if (fds[0].revents & (POLLIN | POLLPRI) &&
                (read_count = read (fds[0].fd, buf, BUFSIZ)) > 0) {
                write (STDERR_FILENO, buf, (size_t)(read_count));
            }
            if (fds[1].revents & (POLLIN | POLLPRI) &&
                (read_count = read (fds[1].fd, buf, BUFSIZ)) > 0) {
                write (STDERR_FILENO, buf, (size_t)(read_count));
            }
            if (fds[2].revents & (POLLIN | POLLPRI) &&
                (read_count = read (fds[2].fd, buf, BUFSIZ)) > 0) {
                write (STDOUT_FILENO, buf, (size_t)(read_count));
            }
            if (fds[3].revents & (POLLIN | POLLPRI)) {
                read (fds[3].fd, (char[]){ 1 }, 1);
                if (waitpid (pid, &child_status, WNOHANG) > 0) {
                    g_assert (WIFEXITED (child_status) || WIFSIGNALED (child_status));
                    child_exited = true;
                }
            }
        }

        if (WIFEXITED (child_status)) {
            exit (WEXITSTATUS (child_status));
        }
        if (WIFSIGNALED (child_status)) {
            exit (WTERMSIG (child_status) + 128);
        }
        g_assert_not_reached ();
    }

    // 以下內容都只有子程序會執行到

    sigaction (SIGCHLD, &act_chld_old, NULL);

    close (sigchld_pipe[0]);
    close (sigchld_pipe[1]);

    if (dup2 (stdout_pipe[1], STDOUT_FILENO) < 0 ||
        dup2 (stderr_pipe[1], STDERR_FILENO) < 0) {
        perror ("dup2");
        exit (EXIT_STATUS_DUP2_ERROR);
    }

    FILE *output_fp = fdopen (output_pipe[1], "w");
    if (output_fp == NULL) {
        perror ("fdopen");
        exit (EXIT_STATUS_STDIO_ERROR);
    }

    if (setvbuf (stdout, NULL, _IONBF, 0) ||
        setvbuf (output_fp, NULL, _IOLBF, 0)) {
        perror ("setvbuf");
        exit (EXIT_STATUS_STDIO_ERROR);
    }

    close (stdout_pipe[0]);
    close (stdout_pipe[1]);
    close (stderr_pipe[0]);
    close (stderr_pipe[1]);
    close (output_pipe[0]);

    // 終於可以開始做事了

    if (!gtk_init_check (&argc, &argv)) {
        g_critical ("無法初始化 GTK+ - 立即離開");
        exit (EXIT_STATUS_GTK_INIT_ERROR);
    }

    HelperData helper_data;
    helper_data_init (&helper_data);

    helper_data.output_fp = output_fp;

    GIOChannel *stdin_channel = g_io_channel_unix_new (0);
    stdin_read (stdin_channel, &(helper_data.login_uri), false);
    stdin_read (stdin_channel, &(helper_data.expected_uri), false);
    helper_data.stdin_channel = g_io_channel_ref (stdin_channel);

    GtkWidget *window_widget = gtk_window_new (GTK_WINDOW_TOPLEVEL);
    GtkWindow *window = GTK_WINDOW (window_widget);
    GObject *window_object = G_OBJECT (window);
    gtk_window_resize (window, 1050, 550);
    g_signal_connect (window, "delete-event",
        G_CALLBACK (window_close_cb), NULL);

    g_assert (g_object_get_data (window_object, "default-title") == NULL);

    char *window_title;
    if (argc >= 2) {
        window_title = g_strjoinv (" - ", argv + 1);
    } else {
        window_title = g_strdup (g_get_prgname ());
    }
    gtk_window_set_title (window, window_title);
    g_object_set_data_full (window_object, "default-title", window_title, g_free);

    WebKitWebContext *web_context = webkit_web_context_new_ephemeral ();
    GtkWidget *web_view_widget = webkit_web_view_new_with_context (web_context);
    WebKitWebView *web_view = WEBKIT_WEB_VIEW (web_view_widget);
    GtkContainer *window_container = GTK_CONTAINER (window_widget);
    gtk_container_add (window_container, web_view_widget);

    helper_data.web_view = g_object_ref (web_view);
    WebKitSettings *settings = webkit_web_view_get_settings (web_view);
    webkit_settings_set_enable_developer_extras (settings, TRUE);

    g_signal_connect (web_view, "decide-policy",
        G_CALLBACK (web_view_decide_policy_cb), NULL);
    g_signal_connect (web_view, "enter-fullscreen",
        G_CALLBACK (web_view_enter_fullscreen_cb), NULL);
    g_signal_connect (web_view, "load-changed",
        G_CALLBACK (web_view_load_changed_cb), &helper_data);
    g_signal_connect (web_view, "load-failed",
        G_CALLBACK (web_view_load_failed_cb), &helper_data);
    g_signal_connect (web_view, "notify::estimated-load-progress",
        G_CALLBACK (web_view_update_load_progress_cb), window);
    webkit_web_view_load_uri (web_view, helper_data.login_uri);

    gtk_widget_show_all (window_widget);

    gtk_main ();

    helper_data_fini (&helper_data);
    gtk_widget_destroy (window_widget);
    g_object_unref (web_context);
    g_io_channel_unref (stdin_channel);

    return 0;
}
