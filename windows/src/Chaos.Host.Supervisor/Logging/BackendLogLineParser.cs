using System.Text.RegularExpressions;
using Chaos.Host.Supervisor.Processes;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Supervisor.Logging;

/// <summary>A child output line, turned into something structured.</summary>
/// <param name="Level">Severity the Python side claimed, or a sane default.</param>
/// <param name="Logger">Python logger name when the line carried one.</param>
/// <param name="Message">The message with the timestamp and level stripped.</param>
public readonly record struct BackendLogLine(LogLevel Level, string? Logger, string Message);

/// <summary>
/// Turns the platform's stdout/stderr into structured log records.
/// </summary>
/// <remarks>
/// The platform logs a lot, in two shapes: <c>logging.basicConfig</c> lines from
/// <c>homestead_twin.cli</c> ("2026-08-07 16:26:45,130 INFO homestead_twin.mqtt: …")
/// and uvicorn's own ("INFO:     Started server process [13780]"). Both go to
/// stderr. Mapping every stderr line to Warning would cry wolf on ordinary INFO
/// traffic; mapping it all to Information would bury a traceback. So we read
/// the level the Python side already stated, and only fall back on the stream
/// when it stated nothing.
/// </remarks>
public static partial class BackendLogLineParser
{
    [GeneratedRegex(
        @"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d+\s+(?<level>[A-Z]{4,8})\s+(?<logger>[\w\.\-]+):\s?(?<message>.*)$",
        RegexOptions.CultureInvariant)]
    private static partial Regex PlatformLine();

    [GeneratedRegex(
        @"^(?<level>CRITICAL|FATAL|ERROR|WARNING|WARN|INFO|DEBUG|TRACE):\s+(?<message>.*)$",
        RegexOptions.CultureInvariant)]
    private static partial Regex UvicornLine();

    public static BackendLogLine Parse(ProcessOutputLine line)
    {
        var text = line.Text;

        var platform = PlatformLine().Match(text);
        if (platform.Success)
        {
            return new BackendLogLine(
                MapLevel(platform.Groups["level"].Value, LogLevel.Information),
                platform.Groups["logger"].Value,
                platform.Groups["message"].Value);
        }

        var uvicorn = UvicornLine().Match(text);
        if (uvicorn.Success)
        {
            return new BackendLogLine(
                MapLevel(uvicorn.Groups["level"].Value, LogLevel.Information),
                null,
                uvicorn.Groups["message"].Value);
        }

        // Python prints tracebacks as bare, multi-line stderr. The header is
        // the one reliable marker that what follows is a crash.
        if (text.StartsWith("Traceback (most recent call last)", StringComparison.Ordinal))
        {
            return new BackendLogLine(LogLevel.Error, null, text);
        }

        var fallback = line.Stream == ProcessOutputStream.StandardError
            ? LogLevel.Warning
            : LogLevel.Information;

        return new BackendLogLine(fallback, null, text);
    }

    private static LogLevel MapLevel(string token, LogLevel fallback) => token switch
    {
        "CRITICAL" or "FATAL" => LogLevel.Critical,
        "ERROR" => LogLevel.Error,
        "WARNING" or "WARN" => LogLevel.Warning,
        "INFO" => LogLevel.Information,
        "DEBUG" => LogLevel.Debug,
        "TRACE" => LogLevel.Trace,
        _ => fallback,
    };
}
