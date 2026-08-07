using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

namespace Chaos.Shell;

/// <summary>
/// A Windows Job Object holding a platform this shell started, and everything
/// that platform spawns.
/// </summary>
/// <remarks>
/// <para>
/// This is a deliberate copy of the same mechanism the backend supervisor uses,
/// for the same reason and with the same shape. It is not shared code because
/// the shell must not take a project reference on the supervisor: the shell is
/// a viewer that can also start things, not part of the platform.
/// </para>
/// <para>
/// Killing a PID kills one process. The gateway supervises a Python child of its
/// own, which would survive as an orphan, keep the loopback port bound, and make
/// the next start fail with "address already in use" — on a homestead node
/// nobody is standing next to. The job is created with
/// <c>JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE</c>, so the tree also dies if this
/// shell is killed rather than closed, which is the case that would otherwise
/// leave a gateway running with nothing watching it.
/// </para>
/// </remarks>
[SupportedOSPlatform("windows")]
internal sealed class WindowsJobObject : IDisposable
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x2000;
    private const uint TerminateExitCode = 1;

    private IntPtr _handle;
    private bool _disposed;

    private WindowsJobObject(IntPtr handle) => _handle = handle;

    /// <summary>
    /// Creates a job configured to kill its members when the handle closes.
    /// Returns null with a reason rather than throwing: running without a job
    /// object is a degraded mode that the log states, not a reason to refuse to
    /// start the platform an operator is asking for.
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

        detail = $"Job object '{name}' created: the platform and anything it starts will be cleaned "
            + "up even if this shell is killed rather than closed.";
        return job;
    }

    public bool TryAssign(Process process, out string detail)
    {
        ArgumentNullException.ThrowIfNull(process);

        if (_handle == IntPtr.Zero)
        {
            detail = "The job object handle is not open.";
            return false;
        }

        IntPtr processHandle;
        try
        {
            processHandle = process.Handle;
        }
        catch (Exception ex) when (ex is InvalidOperationException or Win32Exception)
        {
            detail = $"Could not take a handle on process {Id(process)}: {ex.Message}";
            return false;
        }

        if (!AssignProcessToJobObject(_handle, processHandle))
        {
            detail = $"AssignProcessToJobObject failed for process {Id(process)}: {LastError()}";
            return false;
        }

        detail = $"Process {Id(process)} is in the job object.";
        return true;
    }

    /// <summary>Terminates every process in the job.</summary>
    public bool TryTerminate(out string detail)
    {
        if (_handle == IntPtr.Zero)
        {
            detail = "The job object handle is not open.";
            return false;
        }

        if (!TerminateJobObject(_handle, TerminateExitCode))
        {
            detail = $"TerminateJobObject failed: {LastError()}";
            return false;
        }

        detail = "The platform and every process it started have been terminated.";
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
            // the whole point: no orphaned gateway if this shell dies.
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

    private static string Id(Process process)
    {
        try
        {
            return process.Id.ToString(CultureInfo.InvariantCulture);
        }
        catch (InvalidOperationException)
        {
            return "(unknown)";
        }
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

    // Classic DllImport rather than the [LibraryImport] source generator, which
    // is what the supervisor uses. The generator emits unsafe marshalling stubs
    // and so needs AllowUnsafeBlocks; this project has none, every other
    // P/Invoke in it (TrayIcon, MonitorEnumerator) is a DllImport, and adding a
    // compiler switch to the owner's project for one file is not worth it.

    [DllImport("kernel32.dll", EntryPoint = "CreateJobObjectW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetInformationJobObject(IntPtr job, int informationClass, IntPtr information, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);
}
