using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

namespace Chaos.Host.Supervisor.Processes;

/// <summary>
/// A Windows Job Object holding the Python child and everything it spawns.
/// </summary>
/// <remarks>
/// <para>
/// This is the right tool for the job on Windows and there is no substitute.
/// Killing a PID kills one process; uvicorn's reloader, a multiprocessing
/// worker or anything the platform shells out to would survive as an orphan,
/// keep the loopback port bound, and make the next start fail with "address
/// already in use" — on a machine an operator cannot log into.
/// </para>
/// <para>
/// The job is created with <c>JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE</c>, so the
/// tree also dies if the supervisor itself is killed: closing the handle (even
/// by process teardown) terminates every member. That covers the case that
/// matters most — the service being force-stopped by SCM.
/// </para>
/// <para>
/// Known gap: the child is assigned to the job immediately after
/// <c>Process.Start</c>, not atomically with creation, because
/// <c>ProcessStartInfo</c> cannot pass <c>CREATE_SUSPENDED</c>. A grandchild
/// spawned in the microseconds before assignment would escape. Closing that
/// window needs a raw <c>CreateProcessW</c> P/Invoke including pipe handle
/// inheritance; it is not worth the surface area here, because the Python
/// entry point does no forking before the interpreter is up.
/// </para>
/// </remarks>
[SupportedOSPlatform("windows")]
internal sealed partial class WindowsJobObject : IDisposable
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x2000;
    private const uint TerminateExitCode = 1;

    private IntPtr _handle;
    private bool _disposed;

    private WindowsJobObject(IntPtr handle) => _handle = handle;

    /// <summary>
    /// Creates a job configured to kill its members when the handle closes.
    /// Returns null (with a reason) rather than throwing: no Job Object is a
    /// degraded mode, not a reason to refuse to start the backend.
    /// </summary>
    public static WindowsJobObject? TryCreate(string name, out string detail)
    {
        var handle = CreateJobObject(IntPtr.Zero, name);
        if (handle == IntPtr.Zero)
        {
            detail = $"CreateJobObject failed: {LastError()}";
            return null;
        }

        var job = new WindowsJobObject(handle);
        if (!job.TryConfigureKillOnClose(out var configureDetail))
        {
            job.Dispose();
            detail = configureDetail;
            return null;
        }

        detail = $"job object '{name}' created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE";
        return job;
    }

    public bool TryAssign(Process process, out string detail)
    {
        ArgumentNullException.ThrowIfNull(process);

        if (_handle == IntPtr.Zero)
        {
            detail = "job object handle is not open";
            return false;
        }

        IntPtr processHandle;
        try
        {
            processHandle = process.Handle;
        }
        catch (Exception ex) when (ex is InvalidOperationException or Win32Exception)
        {
            detail = $"could not take a handle on pid {process.Id.ToString(CultureInfo.InvariantCulture)}: {ex.Message}";
            return false;
        }

        if (!AssignProcessToJobObject(_handle, processHandle))
        {
            detail = $"AssignProcessToJobObject failed for pid {process.Id.ToString(CultureInfo.InvariantCulture)}: {LastError()}";
            return false;
        }

        detail = $"pid {process.Id.ToString(CultureInfo.InvariantCulture)} assigned to the job object";
        return true;
    }

    /// <summary>Terminates every process in the job.</summary>
    public bool TryTerminate(out string detail)
    {
        if (_handle == IntPtr.Zero)
        {
            detail = "job object handle is not open";
            return false;
        }

        if (!TerminateJobObject(_handle, TerminateExitCode))
        {
            detail = $"TerminateJobObject failed: {LastError()}";
            return false;
        }

        detail = "terminated the job object: the child and every descendant are gone";
        return true;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        if (_handle != IntPtr.Zero)
        {
            // Closing the last handle terminates the job's members, which is
            // the whole point: no orphans if the supervisor dies.
            CloseHandle(_handle);
            _handle = IntPtr.Zero;
        }
    }

    private bool TryConfigureKillOnClose(out string detail)
    {
        var information = default(JobObjectExtendedLimitInformationStruct);
        information.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;

        var size = Marshal.SizeOf<JobObjectExtendedLimitInformationStruct>();
        var buffer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(information, buffer, fDeleteOld: false);
            if (!SetInformationJobObject(_handle, JobObjectExtendedLimitInformation, buffer, (uint)size))
            {
                detail = $"SetInformationJobObject failed: {LastError()}";
                return false;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }

        detail = "configured";
        return true;
    }

    private static string LastError()
    {
        var code = Marshal.GetLastWin32Error();
        return $"Win32 error {code.ToString(CultureInfo.InvariantCulture)} ({new Win32Exception(code).Message})";
    }

    // -- interop -----------------------------------------------------------

#pragma warning disable CS0649 // Interop layout structs: fields are written by the OS, not by us.

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformationStruct
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

#pragma warning restore CS0649

    [LibraryImport("kernel32.dll", EntryPoint = "CreateJobObjectW", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    private static partial IntPtr CreateJobObject(IntPtr securityAttributes, string? name);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool SetInformationJobObject(IntPtr job, int informationClass, IntPtr information, uint length);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool TerminateJobObject(IntPtr job, uint exitCode);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CloseHandle(IntPtr handle);
}
