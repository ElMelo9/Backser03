# -*- coding: utf-8 -*-
"""
Icono de CaronteSync en el área de notificación de Windows (junto al reloj),
como el de Dropbox: menú con el botón derecho, avance en la descripción emergente
y globos de aviso al terminar una subida.

Tkinter no sabe hacerlo, así que se usa la API de Windows (Shell_NotifyIconW) con
ctypes. La ventana oculta que recibe los clics se crea en el mismo hilo que Tk, y
el bucle de Tk ya despacha sus mensajes: no hace falta otro hilo. Usa la
estructura NOTIFYICONDATA de Windows 2000, que funciona de XP/2003 en adelante.
"""

import ctypes
import os

if os.name != "nt":
    raise ImportError("el icono de la bandeja solo existe en Windows")

from ctypes import wintypes as w  # noqa: E402

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

CLASE_VENTANA = "CaronteSyncBandeja"
WM_NULL = 0x0000
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_BANDEJA = WM_APP + 1      # clics sobre el icono
WM_MOSTRAR = WM_APP + 2      # otra instancia pide mostrar la ventana

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 1, 2, 3
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0000, 0x0001, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXSMICON, SM_CYSMICON = 49, 50
IDI_APPLICATION = 32512


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                ("lpszClassName", w.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    """Versión de Windows 2000 (sin guidItem ni hBalloonIcon), válida de 2000 a 10."""
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                ("uTimeoutOrVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD)]


def _prototipos():
    def p(funcion, resultado, *argumentos):
        funcion.restype = resultado
        funcion.argtypes = argumentos
    p(user32.DefWindowProcW, LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    p(user32.RegisterClassW, w.ATOM, ctypes.POINTER(WNDCLASSW))
    p(user32.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
    p(user32.DestroyWindow, w.BOOL, w.HWND)
    p(user32.RegisterWindowMessageW, w.UINT, w.LPCWSTR)
    p(user32.LoadImageW, w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT)
    p(user32.LoadIconW, w.HICON, w.HINSTANCE, w.LPVOID)
    p(user32.DestroyIcon, w.BOOL, w.HICON)
    p(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    p(user32.CreatePopupMenu, w.HMENU)
    p(user32.AppendMenuW, w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
    p(user32.SetMenuDefaultItem, w.BOOL, w.HMENU, w.UINT, w.UINT)
    p(user32.TrackPopupMenu, w.BOOL, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID)
    p(user32.DestroyMenu, w.BOOL, w.HMENU)
    p(user32.GetCursorPos, w.BOOL, ctypes.POINTER(w.POINT))
    p(user32.SetForegroundWindow, w.BOOL, w.HWND)
    p(user32.PostMessageW, w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    p(user32.FindWindowW, w.HWND, w.LPCWSTR, w.LPCWSTR)
    p(shell32.Shell_NotifyIconW, w.BOOL, w.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    p(kernel32.GetModuleHandleW, w.HMODULE, w.LPCWSTR)


_prototipos()


# Una sola clase de ventana y un solo procedimiento por proceso: Windows guarda el
# procedimiento de la primera clase registrada, así que cada mensaje se reparte a
# su icono según la ventana que lo recibe.
_ICONOS = {}


def _despachar(hwnd, mensaje, wparam, lparam):
    icono = _ICONOS.get(hwnd)
    if icono is not None:
        try:
            resultado = icono._procesar(mensaje, lparam)
            if resultado is not None:
                return resultado
        except Exception:      # una excepción no debe cruzar hacia Windows
            pass
    return user32.DefWindowProcW(hwnd, mensaje, wparam, lparam)


_PROCEDIMIENTO = WNDPROC(_despachar)   # se guarda en el módulo: Windows lo llama mientras viva el proceso
_clase_registrada = False


def _registrar_clase():
    global _clase_registrada
    if _clase_registrada:
        return
    clase = WNDCLASSW()
    clase.lpfnWndProc = _PROCEDIMIENTO
    clase.hInstance = kernel32.GetModuleHandleW(None)
    clase.lpszClassName = CLASE_VENTANA
    if not user32.RegisterClassW(ctypes.byref(clase)):
        raise ctypes.WinError()
    _clase_registrada = True


def mostrar_instancia_abierta():
    """Si CaronteSync ya está abierto (quizá oculto en la bandeja), le pide que se
    muestre y devuelve True: así no se abren dos ventanas."""
    hwnd = user32.FindWindowW(CLASE_VENTANA, None)
    if hwnd:
        user32.PostMessageW(hwnd, WM_MOSTRAR, 0, 0)
        return True
    return False


class IconoBandeja(object):
    """elementos_menu: lista de (texto, función, función_habilitado) o None (separador).
    Las funciones se llaman en el hilo de Tk (dentro de su bucle de mensajes)."""

    def __init__(self, ruta_icono, descripcion, al_abrir, elementos_menu):
        self.al_abrir = al_abrir
        self.elementos = elementos_menu
        self.descripcion = descripcion[:127]
        self._iconos = {}
        _registrar_clase()
        # Ventana normal pero nunca visible: a diferencia de HWND_MESSAGE, recibe el aviso
        # "TaskbarCreated" para volver a poner el icono si el Explorador se reinicia.
        self.hwnd = user32.CreateWindowExW(0, CLASE_VENTANA, "CaronteSync", 0, 0, 0, 0, 0,
                                           None, None, kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise ctypes.WinError()
        _ICONOS[self.hwnd] = self
        self._mensaje_barra = user32.RegisterWindowMessageW("TaskbarCreated")
        self.hicon = self._icono(ruta_icono)
        if not self._notificar(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP):
            raise OSError("Windows no aceptó el icono de la bandeja")

    def _icono(self, ruta):
        if ruta not in self._iconos:
            ancho = user32.GetSystemMetrics(SM_CXSMICON) or 16
            alto = user32.GetSystemMetrics(SM_CYSMICON) or 16
            hicon = user32.LoadImageW(None, ruta, IMAGE_ICON, ancho, alto, LR_LOADFROMFILE) if ruta else None
            self._iconos[ruta] = hicon or user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        return self._iconos[ruta]

    def _datos(self, banderas):
        datos = NOTIFYICONDATAW()
        datos.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        datos.hWnd = self.hwnd
        datos.uID = 1
        datos.uFlags = banderas
        datos.uCallbackMessage = WM_BANDEJA
        datos.hIcon = self.hicon
        datos.szTip = self.descripcion
        return datos

    def _notificar(self, accion, banderas, datos=None):
        datos = datos or self._datos(banderas)
        return bool(shell32.Shell_NotifyIconW(accion, ctypes.byref(datos)))

    def actualizar(self, descripcion=None, ruta_icono=None):
        """Cambia la descripción emergente y/o el icono (solo si cambiaron)."""
        banderas = 0
        if descripcion is not None and descripcion[:127] != self.descripcion:
            self.descripcion = descripcion[:127]
            banderas |= NIF_TIP
        if ruta_icono is not None and self._icono(ruta_icono) != self.hicon:
            self.hicon = self._icono(ruta_icono)
            banderas |= NIF_ICON
        if banderas:
            self._notificar(NIM_MODIFY, banderas)

    def aviso(self, titulo, texto, tipo=NIIF_INFO):
        """Globo de aviso junto al reloj (como "Respaldo subido a la nube")."""
        datos = self._datos(NIF_INFO)
        datos.szInfoTitle = titulo[:63]
        datos.szInfo = texto[:255]
        datos.uTimeoutOrVersion = 10000
        datos.dwInfoFlags = tipo
        return self._notificar(NIM_MODIFY, NIF_INFO, datos)

    def quitar(self):
        if self.hwnd:
            self._notificar(NIM_DELETE, 0)
            _ICONOS.pop(self.hwnd, None)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        for hicon in self._iconos.values():
            user32.DestroyIcon(hicon)
        self._iconos.clear()

    def crear_menu(self):
        menu = user32.CreatePopupMenu()
        for numero, elemento in enumerate(self.elementos, 1):
            if elemento is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            texto, _, habilitado = elemento
            banderas = MF_STRING if habilitado is None or habilitado() else MF_STRING | MF_GRAYED
            user32.AppendMenuW(menu, banderas, numero, texto)
        user32.SetMenuDefaultItem(menu, 1, False)
        return menu

    def ejecutar_elemento(self, numero):
        if 1 <= numero <= len(self.elementos) and self.elementos[numero - 1] is not None:
            self.elementos[numero - 1][1]()

    def _mostrar_menu(self):
        menu = self.crear_menu()
        punto = w.POINT()
        user32.GetCursorPos(ctypes.byref(punto))
        # Sin esto el menú no se cierra al hacer clic fuera (comportamiento documentado de Windows).
        user32.SetForegroundWindow(self.hwnd)
        eleccion = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                                         punto.x, punto.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        self.ejecutar_elemento(eleccion)

    def _procesar(self, mensaje, lparam):
        """Devuelve 0 si atendió el mensaje, o None para el tratamiento normal de Windows."""
        if mensaje == WM_BANDEJA:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.al_abrir()
            elif lparam == WM_RBUTTONUP:
                self._mostrar_menu()
            return 0
        if mensaje == WM_MOSTRAR:
            self.al_abrir()
            return 0
        if mensaje == self._mensaje_barra and self._mensaje_barra:
            self._notificar(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)   # el Explorador se reinició
            return 0
        return None
