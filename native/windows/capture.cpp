#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <ks.h>
#include <ksmedia.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <iterator>
#include <set>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

static void Check(HRESULT status) {
    if (FAILED(status)) throw status;
}

static ComPtr<IMMDeviceEnumerator> Devices() {
    ComPtr<IMMDeviceEnumerator> devices;
    Check(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                          IID_PPV_ARGS(&devices)));
    return devices;
}

static bool ProcessCaptureSupported() {
    using VersionFunction = LONG(WINAPI*)(OSVERSIONINFOW*);
    const auto version_function = reinterpret_cast<VersionFunction>(
        GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "RtlGetVersion"));
    OSVERSIONINFOW version{};
    version.dwOSVersionInfoSize = sizeof(version);
    return version_function && version_function(&version) == 0 && version.dwBuildNumber >= 20348;
}

static void ListSources() {
    std::puts("0\tdefault");
    if (!ProcessCaptureSupported()) return;
    auto devices = Devices();
    ComPtr<IMMDeviceCollection> endpoints;
    Check(devices->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &endpoints));
    UINT endpoint_count = 0;
    Check(endpoints->GetCount(&endpoint_count));
    std::set<DWORD> seen;
    for (UINT index = 0; index < endpoint_count; ++index) {
        ComPtr<IMMDevice> endpoint;
        ComPtr<IAudioSessionManager2> manager;
        ComPtr<IAudioSessionEnumerator> sessions;
        int count = 0;
        if (FAILED(endpoints->Item(index, &endpoint)) ||
            FAILED(endpoint->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr,
                                      reinterpret_cast<void**>(manager.GetAddressOf()))) ||
            FAILED(manager->GetSessionEnumerator(&sessions)) ||
            FAILED(sessions->GetCount(&count))) continue;
        for (int i = 0; i < count; ++i) {
            ComPtr<IAudioSessionControl> session;
            ComPtr<IAudioSessionControl2> details;
            AudioSessionState state{};
            DWORD pid = 0;
            if (FAILED(sessions->GetSession(i, &session)) ||
                FAILED(session.As(&details)) ||
                FAILED(details->GetState(&state)) || state != AudioSessionStateActive ||
                FAILED(details->GetProcessId(&pid)) || pid == 0 || !seen.insert(pid).second) continue;
            wchar_t image[32768]{};
            DWORD size = static_cast<DWORD>(std::size(image));
            HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
            std::wstring name = L"Process";
            if (process) {
                if (QueryFullProcessImageNameW(process, 0, image, &size)) {
                    name = image;
                    name.erase(0, name.find_last_of(L"\\/") + 1);
                }
                CloseHandle(process);
            }
            char utf8[1024]{};
            WideCharToMultiByte(CP_UTF8, 0, name.c_str(), -1, utf8, sizeof(utf8), nullptr, nullptr);
            std::printf("%lu\t%s\n", static_cast<unsigned long>(pid), utf8);
        }
    }
}

class Activation final : public IActivateAudioInterfaceCompletionHandler, public IAgileObject {
public:
    HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HRESULT status = E_FAIL;
    ComPtr<IAudioClient> client;

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** result) override {
        *result = nullptr;
        if (iid == __uuidof(IUnknown) || iid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *result = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
        } else if (iid == __uuidof(IAgileObject)) {
            *result = static_cast<IAgileObject*>(this);
        } else {
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++references; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG remaining = --references;
        if (!remaining) delete this;
        return remaining;
    }
    HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation* operation) override {
        HRESULT activation_status = E_FAIL;
        ComPtr<IUnknown> activated;
        status = operation->GetActivateResult(&activation_status, &activated);
        if (SUCCEEDED(status)) status = activation_status;
        if (SUCCEEDED(status)) status = activated.As(&client);
        SetEvent(done);
        return S_OK;
    }

private:
    ~Activation() { if (done) CloseHandle(done); }
    std::atomic<ULONG> references{1};
};

static ComPtr<IAudioClient> OpenClient(DWORD pid) {
    ComPtr<IAudioClient> client;
    if (!pid) {
        auto devices = Devices();
        ComPtr<IMMDevice> endpoint;
        Check(devices->GetDefaultAudioEndpoint(eRender, eConsole, &endpoint));
        Check(endpoint->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                 reinterpret_cast<void**>(client.GetAddressOf())));
        return client;
    }
    if (!ProcessCaptureSupported()) throw HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED);

    // AUDIOCLIENT_ACTIVATION_PARAMS for process loopback, also usable with older SDK headers.
    struct ProcessLoopbackParams {
        DWORD activation_type = 1;
        DWORD process_id;
        DWORD loopback_mode = 0;
    } params{1, pid, 0};
    PROPVARIANT activation{};
    activation.vt = VT_BLOB;
    activation.blob.cbSize = sizeof(params);
    activation.blob.pBlobData = reinterpret_cast<BYTE*>(&params);
    ComPtr<Activation> callback;
    callback.Attach(new Activation());
    if (!callback->done) throw HRESULT_FROM_WIN32(GetLastError());
    ComPtr<IActivateAudioInterfaceAsyncOperation> operation;
    Check(ActivateAudioInterfaceAsync(L"VAD\\Process_Loopback", __uuidof(IAudioClient),
                                      &activation, callback.Get(), &operation));
    if (WaitForSingleObject(callback->done, 8000) != WAIT_OBJECT_0) {
        throw HRESULT_FROM_WIN32(ERROR_TIMEOUT);
    }
    Check(callback->status);
    return callback->client;
}

static void Capture(DWORD pid) {
    auto client = OpenClient(pid);
    WAVEFORMATEX requested{WAVE_FORMAT_PCM, 2, 44100, 176400, 4, 16, 0};
    WAVEFORMATEX* mix = nullptr;
    if (!pid) Check(client->GetMixFormat(&mix));
    const auto format = mix ? *mix : requested;
    const auto sub_format = mix && format.wFormatTag == WAVE_FORMAT_EXTENSIBLE
        ? reinterpret_cast<WAVEFORMATEXTENSIBLE*>(mix)->SubFormat : GUID{};
    const bool floating = format.wFormatTag == WAVE_FORMAT_IEEE_FLOAT || sub_format == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
    const bool pcm = format.wFormatTag == WAVE_FORMAT_PCM || sub_format == KSDATAFORMAT_SUBTYPE_PCM;
    const HRESULT initialized = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | (pid ? 0x80000000UL : 0UL), 0, 0,
        mix ? mix : &requested, nullptr);
    CoTaskMemFree(mix);
    Check(initialized);
    if (!(floating && format.wBitsPerSample == 32) && !(pcm && format.wBitsPerSample == 16)) {
        throw HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED);
    }
    ComPtr<IAudioCaptureClient> capture;
    Check(client->GetService(IID_PPV_ARGS(&capture)));
    Check(client->Start());

    const HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    const HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    std::vector<short> samples;
    samples.reserve(3200);
    unsigned phase = 0;
    double combined = 0;
    unsigned combined_count = 0;
    ULONGLONG last_output = GetTickCount64();
    while (true) {
        DWORD command_bytes = 0;
        if (GetFileType(input) == FILE_TYPE_PIPE &&
            (!PeekNamedPipe(input, nullptr, 0, nullptr, &command_bytes, nullptr) || command_bytes)) break;
        UINT32 frames = 0;
        Check(capture->GetNextPacketSize(&frames));
        while (frames) {
            BYTE* data = nullptr;
            DWORD flags = 0;
            Check(capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr));
            for (UINT32 frame = 0; frame < frames; ++frame) {
                double sum = 0;
                if (!(flags & AUDCLNT_BUFFERFLAGS_SILENT)) {
                    const BYTE* row = data + frame * format.nBlockAlign;
                    for (WORD channel = 0; channel < format.nChannels; ++channel) {
                        sum += floating ? reinterpret_cast<const float*>(row)[channel]
                                        : reinterpret_cast<const short*>(row)[channel] / 32768.0;
                    }
                }
                combined += sum / format.nChannels;
                ++combined_count;
                phase += 16000;
                if (phase >= format.nSamplesPerSec) {
                    const auto normalized = std::clamp(combined / combined_count, -1.0, 1.0);
                    const auto sample = static_cast<short>(std::lround(normalized * 32767));
                    while (phase >= format.nSamplesPerSec) {
                        samples.push_back(sample);
                        phase -= format.nSamplesPerSec;
                    }
                    combined = 0;
                    combined_count = 0;
                }
            }
            Check(capture->ReleaseBuffer(frames));
            Check(capture->GetNextPacketSize(&frames));
        }
        if (samples.size() >= 1600 || GetTickCount64() - last_output >= 100) {
            if (samples.empty()) samples.resize(1600);
            DWORD written = 0;
            const DWORD length = static_cast<DWORD>(samples.size() * sizeof(short));
            if (!WriteFile(output, samples.data(), length, &written, nullptr) || written != length) break;
            samples.clear();
            last_output = GetTickCount64();
        }
        Sleep(10);
    }
    client->Stop();
}

int main(int argc, char** argv) {
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    int result = 0;
    try {
        Check(initialized);
        if (argc == 2 && std::string(argv[1]) == "list") {
            ListSources();
        } else if (argc == 3 && std::string(argv[1]) == "capture") {
            Capture(static_cast<DWORD>(std::stoul(argv[2])));
        } else {
            result = 2;
        }
    } catch (HRESULT status) {
        std::fprintf(stderr, "WASAPI error 0x%08lX\n", static_cast<unsigned long>(status));
        result = 1;
    } catch (...) {
        result = 2;
    }
    if (SUCCEEDED(initialized)) CoUninitialize();
    return result;
}
