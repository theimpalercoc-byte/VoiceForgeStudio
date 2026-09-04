#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iostream>
#include <string>
#include <filesystem>
#include <thread>
#include <chrono>

#pragma comment(lib, "Ws2_32.lib")
#pragma comment(lib, "Shell32.lib")
#pragma comment(lib, "User32.lib")
#pragma comment(lib, "Kernel32.lib")

namespace fs = std::filesystem;

bool WaitForLocalServerReady(unsigned short port, int maxTimeoutSeconds) {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return false;

    auto startTime = std::chrono::steady_clock::now();
    bool isReady = false;

    while (std::chrono::duration_cast<std::chrono::seconds>(
               std::chrono::steady_clock::now() - startTime).count() < maxTimeoutSeconds) {
        SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (sock != INVALID_SOCKET) {
            u_long mode = 1;
            ioctlsocket(sock, FIONBIO, &mode);

            sockaddr_in addr{};
            addr.sin_family = AF_INET;
            addr.sin_port = htons(port);
            inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

            connect(sock, (sockaddr*)&addr, sizeof(addr));

            fd_set writeSet;
            FD_ZERO(&writeSet);
            FD_SET(sock, &writeSet);
            timeval tv{ 0, 200000 };

            if (select(0, nullptr, &writeSet, nullptr, &tv) > 0) {
                isReady = true;
                closesocket(sock);
                break;
            }
            closesocket(sock);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }

    WSACleanup();
    return isReady;
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, PWSTR pCmdLine, int nCmdShow) {
    // 1. Single-Instance Protection
    HANDLE hMutex = CreateMutexW(NULL, TRUE, L"Local\\VoiceForgeStudio_SingleInstance_Mutex");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        ShellExecuteW(NULL, L"open", L"http://localhost:8080", NULL, NULL, SW_SHOWNORMAL);
        return 0;
    }

    SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);

    wchar_t exeBuffer[MAX_PATH];
    GetModuleFileNameW(NULL, exeBuffer, MAX_PATH);
    fs::path baseDir = fs::path(exeBuffer).parent_path();
    SetCurrentDirectoryW(baseDir.c_str());

    fs::path runtimeDir = baseDir / "runtime";
    fs::path pythonExe = runtimeDir / "python.exe";
    fs::path setupBat = baseDir / "setup.bat";

    // 2. AUTOMATIC FIRST-TIME SETUP (Via native setup.bat)
    if (!fs::exists(pythonExe)) {
        int res = MessageBoxW(NULL,
            L"Welcome to VoiceForge Master Studio!\n\n"
            L"First-time setup is required to download the portable runtime (Python, CUDA PyTorch, and audio libraries).\n\n"
            L"Click OK to begin automatic setup.",
            L"VoiceForge Studio — First-Time Setup",
            MB_OKCANCEL | MB_ICONINFORMATION);

        if (res != IDOK) {
            return 0;
        }

        if (!fs::exists(setupBat)) {
            MessageBoxW(NULL, L"Error: 'setup.bat' not found in the application directory.", L"Setup Error", MB_ICONERROR | MB_OK);
            return 1;
        }

        std::wstring cmdSetup = L"cmd.exe /c \"" + setupBat.wstring() + L"\"";

        STARTUPINFOW siSetup{};
        siSetup.cb = sizeof(siSetup);
        PROCESS_INFORMATION piSetup{};

        BOOL setupLaunched = CreateProcessW(
            NULL,
            &cmdSetup[0],
            NULL,
            NULL,
            FALSE,
            CREATE_NEW_CONSOLE,
            NULL,
            baseDir.c_str(),
            &siSetup,
            &piSetup
        );

        if (!setupLaunched) {
            MessageBoxW(NULL, L"Failed to launch setup.bat. Double-click setup.bat manually.", L"Error", MB_ICONERROR | MB_OK);
            return 1;
        }

        // Wait for setup.bat to finish
        WaitForSingleObject(piSetup.hProcess, INFINITE);
        CloseHandle(piSetup.hProcess);
        CloseHandle(piSetup.hThread);

        if (!fs::exists(pythonExe)) {
            MessageBoxW(NULL,
                L"Setup was closed or did not finish creating 'runtime/python.exe'.\n\n"
                L"Please run 'setup.bat' directly to inspect any network errors.",
                L"Setup Incomplete", MB_ICONERROR | MB_OK);
            return 1;
        }
    }

    fs::path appPy = baseDir / "app.py";
    if (!fs::exists(appPy)) {
        MessageBoxW(NULL, L"Fatal Error: 'app.py' not found in the application directory.", L"Missing File", MB_ICONERROR | MB_OK);
        return 1;
    }

    // 3. Environment Variables
    fs::path sitePackages = runtimeDir / "Lib" / "site-packages";
    fs::path playwrightBrowsers = runtimeDir / "playwright-browsers";

    SetEnvironmentVariableW(L"PYTHONUNBUFFERED", L"1");
    SetEnvironmentVariableW(L"PLAYWRIGHT_BROWSERS_PATH", playwrightBrowsers.c_str());
    SetEnvironmentVariableW(L"HF_HUB_DISABLE_SYMLINKS_WARNING", L"1");

    // 4. Windows Job Object (Child Process Termination Guarantee)
    HANDLE hJob = CreateJobObjectW(NULL, NULL);
    if (hJob) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION jeli{};
        jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK;
        SetInformationJobObject(hJob, JobObjectExtendedLimitInformation, &jeli, sizeof(jeli));
    }

    // 5. Launch Backend Server
    std::wstring cmd = L"\"" + pythonExe.wstring() + L"\" \"" + appPy.wstring() + L"\"";

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};

    DWORD creationFlags = CREATE_NO_WINDOW | CREATE_SUSPENDED;

    BOOL procCreated = CreateProcessW(
        NULL,
        &cmd[0],
        NULL,
        NULL,
        FALSE,
        creationFlags,
        NULL,
        baseDir.c_str(),
        &si,
        &pi
    );

    if (!procCreated) {
        std::wstring err = L"Failed to start Python runtime. Error Code: " + std::to_wstring(GetLastError());
        MessageBoxW(NULL, err.c_str(), L"Engine Startup Error", MB_ICONERROR | MB_OK);
        return 1;
    }

    if (hJob) {
        AssignProcessToJobObject(hJob, pi.hProcess);
    }
    ResumeThread(pi.hThread);

    // 6. Asynchronous Browser Opener
    std::thread([hProcess = pi.hProcess]() {
        if (WaitForLocalServerReady(8080, 45)) {
            ShellExecuteW(NULL, L"open", L"http://localhost:8080", NULL, NULL, SW_SHOWNORMAL);
        } else {
            ShellExecuteW(NULL, L"open", L"http://localhost:8080", NULL, NULL, SW_SHOWNORMAL);
        }
    }).detach();

    // 7. Non-blocking Message Pump
    MSG msg;
    while (true) {
        DWORD dwWait = MsgWaitForMultipleObjectsEx(1, &pi.hProcess, INFINITE, QS_ALLINPUT, MWMO_ALERTABLE);
        if (dwWait == WAIT_OBJECT_0) {
            break;
        }
        while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) break;
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    if (hJob) CloseHandle(hJob);
    if (hMutex) {
        ReleaseMutex(hMutex);
        CloseHandle(hMutex);
    }

    return 0;
}
