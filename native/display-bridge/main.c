/* SPDX-License-Identifier: Apache-2.0
 * Read-only QEMU D-Bus CPU display adapter. No QMP or guest input methods.
 * Bootstrap follows QEMU tests/qtest/dbus-display-test.c (MIT).
 * stdout: BE32 packet size, BE32 JSON size, JSON, optional packed RGBA.
 * stdin EOF is a parent-liveness signal on every platform.
 */
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <sys/socket.h>
#include <unistd.h>
#include <gio/gunixfdlist.h>
#endif
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ROOT "/org/qemu/Display1"
#define IFACE "org.qemu.Display1.Listener"
#define MAX_PIXELS (8192u * 4320u)

static GMainLoop *loop;
static GThread *listener_thread;
static GDBusNodeInfo *node;
static gint qemu_pid, head, inherited_fd = -1;
static GSocket *listener_peer;
static GDBusConnection *primary, *active_listener;
static char *selected_path;
static guint scanout_width, scanout_height;
static gboolean resync_pending;
static void register_listener(void);

static void fail(const char *message) {
    g_printerr("display-bridge: %s\n", message);
    exit(1);
}

static void packet(const char *json, const guint8 *data, gsize length) {
    gsize header_size = strlen(json);
    guint32 sizes[2] = {
        GUINT32_TO_BE((guint32)(4 + header_size + length)),
        GUINT32_TO_BE((guint32)header_size)
    };
    if (fwrite(sizes, 1, sizeof(sizes), stdout) != sizeof(sizes) ||
        fwrite(json, 1, header_size, stdout) != header_size ||
        (length && fwrite(data, 1, length, stdout) != length) || fflush(stdout)) {
        exit(0); /* parent closed its reader */
    }
}

static gpointer parent_watch(gpointer unused) {
    (void)unused;
    while (getchar() != EOF) { }
    exit(0);
    return NULL;
}

static void closed(GDBusConnection *conn, gboolean remote, GError *error, gpointer data) {
    (void)conn; (void)remote; (void)error; (void)data;
    if (conn == primary || conn == active_listener) g_main_loop_quit(loop);
}

static gboolean resync(gpointer data) {
    (void)data;
    GDBusConnection *old = active_listener;
    active_listener = NULL;
    if (old) {
        g_dbus_connection_close(old, NULL, NULL, NULL);
        g_object_unref(old);
    }
    register_listener();
    return G_SOURCE_REMOVE;
}

static void invalidate_surface(void) {
    if (resync_pending) return;
    resync_pending = TRUE;
    packet("{\"kind\":\"disable\"}", NULL, 0);
    g_idle_add(resync, NULL);
}

static void geometry_changed(GDBusConnection *conn, const char *sender, const char *path,
    const char *iface, const char *signal, GVariant *parameters, gpointer data) {
    (void)conn; (void)sender; (void)path; (void)iface; (void)signal; (void)data;
    const char *changed_iface;
    GVariant *changed, *invalidated;
    g_variant_get(parameters, "(&s@a{sv}@as)", &changed_iface, &changed, &invalidated);
    if (!strcmp(changed_iface, "org.qemu.Display1.Console") && scanout_width && scanout_height) {
        guint width = scanout_width, height = scanout_height;
        g_variant_lookup(changed, "Width", "u", &width);
        g_variant_lookup(changed, "Height", "u", &height);
        if (width != scanout_width || height != scanout_height) invalidate_surface();
    }
    g_variant_unref(changed); g_variant_unref(invalidated);
}

static GDBusConnection *connection(GSocket *socket) {
    GError *error = NULL;
    GSocketConnection *stream = g_socket_connection_factory_create_connection(socket);
    GDBusConnection *conn = g_dbus_connection_new_sync(G_IO_STREAM(stream), NULL,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
        G_DBUS_CONNECTION_FLAGS_DELAY_MESSAGE_PROCESSING, NULL, NULL, &error);
    g_object_unref(stream);
    if (!conn) fail(error->message);
    g_signal_connect(conn, "closed", G_CALLBACK(closed), NULL);
    return conn;
}

static gpointer listener_connect(gpointer data) {
    GDBusConnection *conn = connection(G_SOCKET(data));
    g_object_unref(data);
    return conn;
}

static GVariant *properties(GDBusConnection *conn, const char *path, const char *iface) {
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_sync(conn, NULL, path,
        "org.freedesktop.DBus.Properties", "GetAll", g_variant_new("(s)", iface),
        G_VARIANT_TYPE("(a{sv})"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &error);
    if (!reply) fail(error->message);
    GVariant *result = g_variant_get_child_value(reply, 0);
    g_variant_unref(reply);
    return result;
}

static char *console_path(GDBusConnection *conn) {
    GVariant *props = properties(conn, ROOT "/VM", "org.qemu.Display1.VM");
    GVariant *ids = g_variant_lookup_value(props, "ConsoleIDs", G_VARIANT_TYPE("au"));
    if (!ids) fail("console identifiers unavailable");
    GVariantIter iter;
    guint32 id;
    char *selected = NULL;
    g_variant_iter_init(&iter, ids);
    while (g_variant_iter_next(&iter, "u", &id)) {
        char *path = g_strdup_printf(ROOT "/Console_%u", id);
        GVariant *info = properties(conn, path, "org.qemu.Display1.Console");
        guint32 actual_head;
        const char *type = NULL;
        if (g_variant_lookup(info, "Head", "u", &actual_head) &&
            g_variant_lookup(info, "Type", "&s", &type) &&
            !strcmp(type, "Graphic") && actual_head == (guint)head) {
            if (selected) fail("ambiguous graphical head");
            selected = g_strdup(path);
        }
        g_variant_unref(info);
        g_free(path);
    }
    g_variant_unref(ids);
    g_variant_unref(props);
    if (!selected) fail("graphical head unavailable");
    return selected;
}

static void pixels(const char *kind, gint x, gint y, gint width, gint height,
                   guint stride, guint format, GVariant *array) {
    gsize size;
    const guint8 *input = g_variant_get_fixed_array(array, &size, 1);
    guint type = (format >> 16) & 0x3f;
    if (width <= 0 || height <= 0 || width > 8192 || height > 4320 ||
        (guint64)width * height > MAX_PIXELS || stride < (guint)width * 4 ||
        (guint64)stride * height > size || format >> 24 != 32 ||
        (format & 0xfff) != 0x888 || (type != 2 && type != 3)) {
        fail("unsupported or malformed CPU pixel format");
    }
    guint8 *rgba = g_malloc((gsize)width * height * 4);
    for (gint row = 0; row < height; row++) {
        for (gint column = 0; column < width; column++) {
            guint32 pixel;
            memcpy(&pixel, input + (gsize)row * stride + column * 4, 4);
            guint8 *out = rgba + ((gsize)row * width + column) * 4;
            out[0] = (guint8)(pixel >> (type == 2 ? 16 : 0));
            out[1] = (guint8)(pixel >> 8);
            out[2] = (guint8)(pixel >> (type == 2 ? 0 : 16));
            out[3] = 255;
        }
    }
    char *json = g_strdup_printf(
        "{\"kind\":\"%s\",\"x\":%d,\"y\":%d,\"width\":%d,\"height\":%d}",
        kind, x, y, width, height);
    packet(json, rgba, (gsize)width * height * 4);
    g_free(json);
    g_free(rgba);
}

static void method(GDBusConnection *conn, const char *sender, const char *path,
                   const char *iface, const char *name, GVariant *args,
                   GDBusMethodInvocation *invocation, gpointer data) {
    (void)conn; (void)sender; (void)path; (void)iface; (void)data;
    if (conn != active_listener || resync_pending) {
        g_dbus_method_invocation_return_value(invocation, NULL);
        return;
    }
    if (!strcmp(name, "Scanout")) {
        guint width, height, stride, format;
        GVariant *bytes;
        g_variant_get(args, "(uuuu@ay)", &width, &height, &stride, &format, &bytes);
        scanout_width = width; scanout_height = height;
        pixels("scanout", 0, 0, (gint)width, (gint)height, stride, format, bytes);
        g_variant_unref(bytes);
    } else if (!strcmp(name, "Update")) {
        gint x, y, width, height;
        guint stride, format;
        GVariant *bytes;
        g_variant_get(args, "(iiiiuu@ay)", &x, &y, &width, &height, &stride, &format, &bytes);
        if (x < 0 || y < 0 || width <= 0 || height <= 0 ||
            (guint64)x + width > scanout_width || (guint64)y + height > scanout_height) {
            /* QEMU's CPU backend can issue partial rows before a full scanout
             * after a mode switch. Re-register to request a complete surface;
             * never invent pixels outside the previous surface. */
            invalidate_surface();
            g_variant_unref(bytes);
            g_dbus_method_invocation_return_value(invocation, NULL);
            return;
        }
        pixels("update", x, y, width, height, stride, format, bytes);
        g_variant_unref(bytes);
    } else if (!strcmp(name, "Disable")) {
        packet("{\"kind\":\"disable\"}", NULL, 0);
    } else if (!strcmp(name, "MouseSet")) {
        gint x, y, on;
        g_variant_get(args, "(iii)", &x, &y, &on);
        char *json = g_strdup_printf("{\"kind\":\"cursor\",\"x\":%d,\"y\":%d,\"visible\":%s}",
            x, y, on ? "true" : "false");
        packet(json, NULL, 0);
        g_free(json);
    } else if (!strcmp(name, "CursorDefine")) {
        gint width, height, hot_x, hot_y;
        GVariant *bytes;
        gsize size;
        g_variant_get(args, "(iiii@ay)", &width, &height, &hot_x, &hot_y, &bytes);
        const guint8 *input = g_variant_get_fixed_array(bytes, &size, 1);
        if (width <= 0 || height <= 0 || width > 128 || height > 128 ||
            hot_x < 0 || hot_y < 0 || hot_x >= width || hot_y >= height ||
            size != (gsize)width * height * 4) fail("invalid cursor shape");
        guint8 *rgba = g_malloc(size);
        for (gsize i = 0; i < size; i += 4) {
            guint32 pixel;
            memcpy(&pixel, input + i, 4);
            rgba[i] = pixel >> 16; rgba[i+1] = pixel >> 8;
            rgba[i+2] = pixel; rgba[i+3] = pixel >> 24;
        }
        char *encoded = g_base64_encode(rgba, size);
        char *json = g_strdup_printf(
            "{\"kind\":\"cursor\",\"width\":%d,\"height\":%d,\"hot_x\":%d,\"hot_y\":%d,\"rgba\":\"%s\"}",
            width, height, hot_x, hot_y, encoded);
        packet(json, NULL, 0);
        g_free(json); g_free(encoded); g_free(rgba); g_variant_unref(bytes);
    } else {
        g_dbus_method_invocation_return_dbus_error(invocation,
            "org.freedesktop.DBus.Error.UnknownMethod", "CPU display only");
        return;
    }
    g_dbus_method_invocation_return_value(invocation, NULL);
}

static GVariant *property(GDBusConnection *conn, const char *sender, const char *path,
                          const char *iface, const char *name, GError **error, gpointer data) {
    (void)conn; (void)sender; (void)path; (void)iface; (void)name; (void)error; (void)data;
    return g_variant_new_strv(NULL, 0); /* no Map or GPU interfaces */
}

static const GDBusInterfaceVTable vtable = { .method_call = method, .get_property = property };

static void registered(GObject *object, GAsyncResult *result, gpointer data) {
    (void)data;
    GError *error = NULL;
#ifdef _WIN32
    GVariant *reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(object), result, &error);
#else
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(object), NULL, result, &error);
#endif
    if (!reply) fail(error->message);
    g_variant_unref(reply);
    active_listener = g_thread_join(listener_thread);
    if (!g_dbus_connection_register_object(active_listener, ROOT "/Listener", node->interfaces[0],
        &vtable, NULL, NULL, &error)) fail(error->message);
    resync_pending = FALSE;
    scanout_width = scanout_height = 0;
    g_dbus_connection_start_message_processing(active_listener);
    packet("{\"kind\":\"ready\"}", NULL, 0);
}

static void pair(GSocket **local, GSocket **remote) {
    GError *error = NULL;
#ifdef _WIN32
    GSocket *server = g_socket_new(G_SOCKET_FAMILY_IPV4, G_SOCKET_TYPE_STREAM,
        G_SOCKET_PROTOCOL_TCP, &error);
    GInetAddress *loopback = g_inet_address_new_loopback(G_SOCKET_FAMILY_IPV4);
    GSocketAddress *address = g_inet_socket_address_new(loopback, 0);
    if (!server || !g_socket_bind(server, address, FALSE, &error) ||
        !g_socket_listen(server, &error)) fail("listener socket pair failed");
    g_object_unref(address); g_object_unref(loopback);
    address = g_socket_get_local_address(server, &error);
    *local = g_socket_new(G_SOCKET_FAMILY_IPV4, G_SOCKET_TYPE_STREAM, G_SOCKET_PROTOCOL_TCP, &error);
    if (!*local || !g_socket_connect(*local, address, NULL, &error)) fail("socket pair connect failed");
    *remote = g_socket_accept(server, NULL, &error);
    if (!*remote) fail(error->message);
    GSocketAddress *expected = g_socket_get_local_address(*local, &error);
    GSocketAddress *actual = g_socket_get_remote_address(*remote, &error);
    if (!expected || !actual || g_inet_socket_address_get_port(G_INET_SOCKET_ADDRESS(expected)) !=
        g_inet_socket_address_get_port(G_INET_SOCKET_ADDRESS(actual))) fail("socket pair peer mismatch");
    g_object_unref(expected); g_object_unref(actual); g_object_unref(address); g_object_unref(server);
#else
    int fds[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fds)) fail("socketpair failed");
    *local = g_socket_new_from_fd(fds[0], &error);
    *remote = g_socket_new_from_fd(fds[1], &error);
    if (!*local || !*remote) fail("socketpair wrapping failed");
#endif
}

static void register_listener(void) {
    GSocket *local;
    pair(&local, &listener_peer);
    listener_thread = g_thread_new("display-listener", listener_connect, local);
#ifdef _WIN32
    WSAPROTOCOL_INFOW info;
    if (WSADuplicateSocketW((SOCKET)g_socket_get_fd(listener_peer), qemu_pid, &info))
        fail("listener socket duplication failed");
    GVariant *value = g_variant_new_fixed_array(G_VARIANT_TYPE_BYTE, &info, sizeof(info), 1);
    g_dbus_connection_call(primary, NULL, selected_path, "org.qemu.Display1.Console", "RegisterListener",
        g_variant_new("(@ay)", value), NULL, G_DBUS_CALL_FLAGS_NONE, 5000, NULL, registered, NULL);
#else
    GError *error = NULL;
    GUnixFDList *list = g_unix_fd_list_new();
    int index = g_unix_fd_list_append(list, g_socket_get_fd(listener_peer), &error);
    if (index < 0) fail(error->message);
    g_dbus_connection_call_with_unix_fd_list(primary, NULL, selected_path, "org.qemu.Display1.Console",
        "RegisterListener", g_variant_new("(h)", index), NULL, G_DBUS_CALL_FLAGS_NONE,
        5000, list, NULL, registered, NULL);
    g_object_unref(list);
#endif
    g_object_unref(listener_peer);
}

int main(int argc, char **argv) {
    GError *error = NULL;
    GOptionEntry options[] = {
        { "qemu-pid", 0, 0, G_OPTION_ARG_INT, &qemu_pid, "QEMU PID for socket transfer", "PID" },
        { "head", 0, 0, G_OPTION_ARG_INT, &head, "Graphical head", "HEAD" },
        { "fd", 0, 0, G_OPTION_ARG_INT, &inherited_fd, "Inherited POSIX socket", "FD" },
        { NULL, 0, 0, 0, NULL, NULL, NULL }
    };
    GOptionContext *context = g_option_context_new("- Palette experimental read-only display");
    g_option_context_add_main_entries(context, options, NULL);
    if (!g_option_context_parse(context, &argc, &argv, &error)) fail(error->message);
    if (qemu_pid <= 0 || head < 0) fail("invalid bootstrap arguments");
#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa)) fail("Winsock unavailable");
    _setmode(_fileno(stdin), _O_BINARY); _setmode(_fileno(stdout), _O_BINARY);
    char bootstrap[4096];
    if (!fgets(bootstrap, sizeof(bootstrap), stdin)) fail("missing socket bootstrap");
    g_strchomp(bootstrap);
    gsize info_size;
    guchar *bytes = g_base64_decode(bootstrap, &info_size);
    if (info_size != sizeof(WSAPROTOCOL_INFOW)) fail("invalid socket bootstrap");
    SOCKET native = WSASocketW(FROM_PROTOCOL_INFO, FROM_PROTOCOL_INFO, FROM_PROTOCOL_INFO,
        (WSAPROTOCOL_INFOW *)bytes, 0, WSA_FLAG_OVERLAPPED);
    g_free(bytes);
    if (native == INVALID_SOCKET) fail("socket bootstrap failed");
    GSocket *socket = g_socket_new_from_fd((gint)native, &error);
#else
    if (inherited_fd < 0) fail("missing socket descriptor");
    GSocket *socket = g_socket_new_from_fd(inherited_fd, &error);
#endif
    if (!socket) fail(error->message);
    g_thread_unref(g_thread_new("parent-liveness", parent_watch, NULL));
    loop = g_main_loop_new(NULL, FALSE);
    primary = connection(socket);
    g_dbus_connection_start_message_processing(primary);
    selected_path = console_path(primary);
    g_dbus_connection_signal_subscribe(primary, NULL, "org.freedesktop.DBus.Properties",
        "PropertiesChanged", selected_path, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
        geometry_changed, NULL, NULL);
    node = g_dbus_node_info_new_for_xml(
        "<node><interface name='" IFACE "'>"
        "<method name='Scanout'><arg type='u'/><arg type='u'/><arg type='u'/><arg type='u'/><arg type='ay'/></method>"
        "<method name='Update'><arg type='i'/><arg type='i'/><arg type='i'/><arg type='i'/><arg type='u'/><arg type='u'/><arg type='ay'/></method>"
        "<method name='Disable'/>"
        "<method name='MouseSet'><arg type='i'/><arg type='i'/><arg type='i'/></method>"
        "<method name='CursorDefine'><arg type='i'/><arg type='i'/><arg type='i'/><arg type='i'/><arg type='ay'/></method>"
        "<property name='Interfaces' type='as' access='read'/>"
        "</interface></node>", &error);
    if (!node) fail(error->message);
    register_listener();
    g_main_loop_run(loop);
    return 0;
}
