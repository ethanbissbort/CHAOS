using System.Diagnostics;
using System.Globalization;
using System.Runtime.Versioning;
using System.Security;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Supervisor.Events;

/// <summary>
/// Chooses an event sink for this machine.
/// </summary>
/// <remarks>
/// <para>
/// The Windows Event Log is the right place for service lifecycle events: it
/// survives the service crashing, an operator can read it with no tooling, and
/// monitoring already watches it. But it needs a registered <em>source</em>,
/// and creating one needs administrator rights — which a service running as a
/// virtual account does not have.
/// </para>
/// <para>
/// So the rule is: use it if it is there, say so if it is not, and never let
/// either outcome stop the backend from being supervised. A control system that
/// refuses to run because it could not write a log entry has its priorities
/// backwards.
/// </para>
/// </remarks>
public static class ChaosEventSinkFactory
{
    /// <summary>
    /// Builds the best sink available. Never throws, never returns null.
    /// </summary>
    public static IChaosEventSink Create(BackendSupervisorOptions options, ILogger logger)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(logger);

        var fallback = new LoggerEventSink(logger);

        if (!OperatingSystem.IsWindows())
        {
            return new LoggerEventSink(
                logger,
                $"standard ILogger only — the Windows Event Log does not exist on " +
                $"{RuntimeDescription()}, so lifecycle events go to the host's logging pipeline");
        }

        return CreateWindowsSink(options, logger, fallback);
    }

    [SupportedOSPlatform("windows")]
    private static IChaosEventSink CreateWindowsSink(
        BackendSupervisorOptions options,
        ILogger logger,
        LoggerEventSink fallback)
    {
        var source = options.EventLogSource;

        try
        {
            if (EventLog.SourceExists(source))
            {
                return new WindowsEventLogSink(source, options.EventLogName, fallback);
            }

            if (!options.CreateEventLogSourceIfMissing)
            {
                logger.LogWarning(
                    "Windows Event Log source '{Source}' is not registered, so service lifecycle events " +
                    "will go to the application log pipeline only. Register it once, elevated, at install " +
                    "time: New-EventLog -LogName {LogName} -Source '{Source}'.",
                    source,
                    options.EventLogName,
                    source);
                return fallback.WithDescription(
                    $"standard ILogger only — Event Log source '{source}' is not registered and " +
                    $"{nameof(BackendSupervisorOptions.CreateEventLogSourceIfMissing)} is off");
            }

            EventLog.CreateEventSource(new EventSourceCreationData(source, options.EventLogName));
            logger.LogInformation(
                "Created Windows Event Log source '{Source}' in log '{LogName}'.",
                source,
                options.EventLogName);
            return new WindowsEventLogSink(source, options.EventLogName, fallback);
        }
        catch (Exception ex) when (ex is SecurityException or UnauthorizedAccessException or InvalidOperationException or System.ComponentModel.Win32Exception or ArgumentException)
        {
            // The documented failure: SourceExists and CreateEventSource both
            // read/write HKLM\SYSTEM\CurrentControlSet\Services\EventLog, which
            // an unelevated or virtual-account service cannot touch.
            logger.LogWarning(
                ex,
                "Windows Event Log source '{Source}' is unavailable and could not be created ({Reason}). " +
                "Service lifecycle events will go to the application log pipeline only. This is not fatal; " +
                "register the source at install time, elevated, to get them into the Event Viewer.",
                source,
                ex.Message);

            var degraded = fallback.WithDescription(
                $"standard ILogger only — Event Log source '{source}' could not be opened or created " +
                $"({ex.GetType().Name}: {ex.Message})");
            degraded.Write(
                ChaosEventId.EventLogUnavailable,
                ChaosEventLevel.Warning,
                $"Windows Event Log source '{source}' unavailable: {ex.Message}");
            return degraded;
        }
    }

    private static string RuntimeDescription() =>
        System.Runtime.InteropServices.RuntimeInformation.OSDescription;
}

/// <summary>
/// Lifecycle events through <c>ILogger</c>. The only sink off Windows, and the
/// fallback on Windows when the Event Log source is missing.
/// </summary>
public sealed class LoggerEventSink : IChaosEventSink
{
    private readonly ILogger _logger;

    public LoggerEventSink(ILogger logger)
        : this(logger, "standard ILogger")
    {
    }

    public LoggerEventSink(ILogger logger, string description)
    {
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        Description = description;
    }

    public string Description { get; private set; }

    internal LoggerEventSink WithDescription(string description)
    {
        Description = description;
        return this;
    }

    public void Write(ChaosEventId id, ChaosEventLevel level, string message)
    {
        var logLevel = level switch
        {
            ChaosEventLevel.Error => LogLevel.Error,
            ChaosEventLevel.Warning => LogLevel.Warning,
            _ => LogLevel.Information,
        };

        _logger.Log(
            logLevel,
            new EventId((int)id, id.ToString()),
            "[CHAOS {EventId} {EventName}] {Message}",
            ((int)id).ToString(CultureInfo.InvariantCulture),
            id.ToString(),
            message);
    }
}

/// <summary>
/// Writes the Windows Event Log, and mirrors every entry to <c>ILogger</c> so
/// the two never disagree about what happened.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class WindowsEventLogSink : IChaosEventSink
{
    private readonly string _source;
    private readonly LoggerEventSink _mirror;
    private bool _writeFailed;

    internal WindowsEventLogSink(string source, string logName, LoggerEventSink mirror)
    {
        _source = source;
        _mirror = mirror;
        Description = $"Windows Event Log (log '{logName}', source '{source}') and the standard ILogger";
    }

    public string Description { get; }

    public void Write(ChaosEventId id, ChaosEventLevel level, string message)
    {
        _mirror.Write(id, level, message);

        if (_writeFailed)
        {
            // Already told the operator once; do not turn a broken Event Log
            // into a per-event storm.
            return;
        }

        try
        {
            EventLog.WriteEntry(_source, message, ToEntryType(level), (int)id);
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or ArgumentException or SecurityException)
        {
            _writeFailed = true;
            _mirror.Write(
                ChaosEventId.EventLogUnavailable,
                ChaosEventLevel.Warning,
                $"Writing to the Windows Event Log failed ({ex.GetType().Name}: {ex.Message}). " +
                "Further lifecycle events will appear in the application log only.");
        }
    }

    private static EventLogEntryType ToEntryType(ChaosEventLevel level) => level switch
    {
        ChaosEventLevel.Error => EventLogEntryType.Error,
        ChaosEventLevel.Warning => EventLogEntryType.Warning,
        _ => EventLogEntryType.Information,
    };
}
