using Microsoft.Extensions.Options;

namespace Chaos.Host.Configuration;

/// <summary>
/// Validates <see cref="ChaosHostOptions"/> at startup, before anything binds a
/// socket or starts a child process.
/// </summary>
/// <remarks>
/// Every failure here is fatal. A gateway with a misconfigured backend address
/// or a zero timeout does not degrade gracefully — it proxies the control plane
/// somewhere unintended, or hangs, and both are worse than not starting.
/// </remarks>
internal sealed class ChaosHostOptionsValidator : IValidateOptions<ChaosHostOptions>
{
    public ValidateOptionsResult Validate(string? name, ChaosHostOptions options)
    {
        var failures = new List<string>();

        if (string.IsNullOrWhiteSpace(options.ListenUrl))
        {
            failures.Add("Chaos:ListenUrl must not be empty. The gateway is the only LAN listener; it has to bind something.");
        }
        else
        {
            foreach (var url in options.ListenUrl.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            {
                if (!Uri.TryCreate(url, UriKind.Absolute, out _))
                {
                    failures.Add($"Chaos:ListenUrl contains '{url}', which is not an absolute URL.");
                }
            }
        }

        if (!Uri.TryCreate(options.BackendUrl, UriKind.Absolute, out var backend))
        {
            failures.Add($"Chaos:BackendUrl must be an absolute URL; got '{options.BackendUrl}'.");
        }
        else
        {
            if (backend.Scheme is not ("http" or "https"))
            {
                failures.Add($"Chaos:BackendUrl must use http or https; got scheme '{backend.Scheme}'.");
            }

            if (!options.AllowNonLoopbackBackend && !IsLoopback(backend))
            {
                failures.Add(
                    $"Chaos:BackendUrl '{options.BackendUrl}' is not a loopback address. The Python backend is a "
                  + "child process that must not be reachable from the LAN - the gateway is the only listener. "
                  + "Set Chaos:AllowNonLoopbackBackend=true only if you have deliberately decided to widen that "
                  + "trust boundary and have firewalled the backend yourself.");
            }
        }

        RequirePositive(failures, nameof(ChaosHostOptions.RequestTimeout), options.RequestTimeout);
        RequirePositive(failures, nameof(ChaosHostOptions.BackendStartTimeout), options.BackendStartTimeout);
        RequirePositive(failures, nameof(ChaosHostOptions.ShutdownTimeout), options.ShutdownTimeout);
        RequirePositive(failures, nameof(ChaosHostOptions.BackendHealthInterval), options.BackendHealthInterval);
        RequirePositive(failures, nameof(ChaosHostOptions.BackendHealthMaxBackoff), options.BackendHealthMaxBackoff);
        RequirePositive(failures, nameof(ChaosHostOptions.BackendHealthTimeout), options.BackendHealthTimeout);

        if (string.IsNullOrWhiteSpace(options.BackendHealthPath) || !options.BackendHealthPath.StartsWith('/'))
        {
            failures.Add($"Chaos:BackendHealthPath must be an absolute path starting with '/'; got '{options.BackendHealthPath}'.");
        }

        RequirePositive(failures, nameof(ChaosHostOptions.SetupTimeout), options.SetupTimeout);
        RequirePositive(failures, nameof(ChaosHostOptions.SetupProbeTimeout), options.SetupProbeTimeout);
        RequirePositive(failures, nameof(ChaosHostOptions.SetupCommandTimeout), options.SetupCommandTimeout);

        if (options.SetupTimeout < options.SetupCommandTimeout)
        {
            failures.Add(
                $"Chaos:SetupTimeout ({options.SetupTimeout}) must be at least Chaos:SetupCommandTimeout "
              + $"({options.SetupCommandTimeout}); otherwise the run budget expires mid-import and the operator "
              + "is told setup timed out when in fact it was never given time to run.");
        }

        return failures.Count == 0
            ? ValidateOptionsResult.Success
            : ValidateOptionsResult.Fail(failures);
    }

    private static void RequirePositive(List<string> failures, string property, TimeSpan value)
    {
        if (value <= TimeSpan.Zero)
        {
            failures.Add($"Chaos:{property} must be greater than zero; got {value}.");
        }
    }

    private static bool IsLoopback(Uri uri)
    {
        if (uri.IsLoopback)
        {
            return true;
        }

        // Uri.IsLoopback covers "localhost", 127.0.0.0/8 and ::1. Anything else
        // is reachable from somewhere other than this machine as far as we know,
        // and we do not guess in the reassuring direction.
        return false;
    }
}
