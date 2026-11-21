/*
 * Нативный лаунчер Eve's Text для Windows 11.
 *
 * Это обычное GUI-приложение (не pythonw.exe с параметрами), поэтому его
 * можно выбрать в «Приложениях по умолчанию» и связать с .txt и .rtf.
 * Лаунчер только готовит окружение и запускает встроенный Python с
 * windows/launcher.py, передавая ему путь к файлу, если файл открыли
 * через «Открыть с помощью».
 */

#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shellapi.h>

#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define APP_USER_MODEL_ID L"DanielUsov.EvesText"

static void append_quoted(wchar_t *dst, size_t cap, const wchar_t *arg) {
    size_t used = wcslen(dst);
    if (used + 1 >= cap) {
        return;
    }
    dst[used++] = L'"';
    for (const wchar_t *p = arg; *p && used + 2 < cap; ++p) {
        if (*p == L'"') {
            /* В command line Windows кавычка экранируется обратными слешами. */
            dst[used++] = L'\\';
        }
        dst[used++] = *p;
    }
    dst[used++] = L'"';
    dst[used] = L'\0';
}

static void set_app_user_model_id(void) {
    typedef HRESULT(WINAPI * SetAumidFn)(PCWSTR);
    HMODULE shell = LoadLibraryW(L"shell32.dll");
    if (shell == NULL) {
        return;
    }
    SetAumidFn fn =
        (SetAumidFn)GetProcAddress(shell, "SetCurrentProcessExplicitAppUserModelID");
    if (fn != NULL) {
        fn(APP_USER_MODEL_ID);
    }
    FreeLibrary(shell);
}

static int show_error(const wchar_t *message) {
    MessageBoxW(NULL, message, L"Eve's Text", MB_ICONERROR | MB_OK);
    return 1;
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                    PWSTR lpCmdLine, int nCmdShow) {
    (void)hInstance;
    (void)hPrevInstance;
    (void)lpCmdLine;
    (void)nCmdShow;

    set_app_user_model_id();

    wchar_t root[MAX_PATH];
    if (GetModuleFileNameW(NULL, root, MAX_PATH) == 0) {
        return show_error(L"Не удалось определить путь к приложению.");
    }
    wchar_t *slash = wcsrchr(root, L'\\');
    if (slash == NULL) {
        return show_error(L"Не удалось определить папку приложения.");
    }
    *slash = L'\0';

    wchar_t python[MAX_PATH];
    wchar_t script[MAX_PATH];
    wchar_t env_path[MAX_PATH];
    swprintf(python, MAX_PATH, L"%ls\\bin\\pythonw.exe", root);
    swprintf(script, MAX_PATH, L"%ls\\app\\launcher.py", root);

    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES) {
        return show_error(L"Не найден встроенный Python: bin\\pythonw.exe");
    }
    if (GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        return show_error(L"Не найден загрузчик приложения: app\\launcher.py");
    }

    /* launcher.py сам вычисляет путь бандла, но явная переменная упрощает
       диагностику и позволяет нему GUI-вызовам найти корень приложения. */
    swprintf(env_path, MAX_PATH, L"%ls\\app", root);
    SetEnvironmentVariableW(L"EVESTEXT_ROOT", root);
    SetEnvironmentVariableW(L"PYTHONPATH", env_path);

    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv == NULL) {
        return show_error(L"Не удалось разобрать аргументы командной строки.");
    }

    wchar_t command[16384];
    command[0] = L'\0';
    append_quoted(command, 16384, python);
    wcscat(command, L" ");
    append_quoted(command, 16384, script);
    for (int i = 1; i < argc; ++i) {
        wcscat(command, L" ");
        append_quoted(command, 16384, argv[i]);
    }
    LocalFree(argv);

    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.cb = sizeof(startup);

    if (!CreateProcessW(NULL, command, NULL, NULL, FALSE, 0, NULL, root,
                        &startup, &process)) {
        wchar_t message[512];
        swprintf(message, 512, L"Не удалось запустить Eve's Text (код %lu).",
                 (unsigned long)GetLastError());
        return show_error(message);
    }

    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    return (int)code;
}
