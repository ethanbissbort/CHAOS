using System.Net;

namespace Chaos.Shell.Core;

/// <summary>
/// Decides whether the configured gateway address names the machine the shell
/// is running on.
/// </summary>
/// <remarks>
/// This gates every lifecycle control the launcher offers. A shell pointed at
/// <c>chaos-node.lan</c> from a laptop can watch that platform perfectly well,
/// but it cannot start it, stop it, or run its setup — and offering a Start
/// button that launches a local process the operator will then not be able to
/// find is worse than offering nothing. Nothing here touches the network: the
/// facts about this machine are passed in, so the rule is testable.
/// </remarks>
public static class LocalAddress
{
    /// <summary>Names that always mean this machine.</summary>
    private static readonly string[] AlwaysLocal = { "localhost", ".", "(local)" };

    /// <summary>
    /// Whether <paramref name="address"/> is this machine.
    /// </summary>
    /// <param name="address">The configured host name or IP.</param>
    /// <param name="machineName">This machine's name, e.g. <c>Environment.MachineName</c>.</param>
    /// <param name="localAddresses">
    /// The IP addresses bound on this machine. Empty is fine: the loopback and
    /// name rules still apply, and the answer only gets more conservative.
    /// </param>
    public static bool IsThisMachine(
        string? address,
        string machineName,
        IReadOnlyList<string>? localAddresses = null)
    {
        var text = address?.Trim();
        if (string.IsNullOrEmpty(text))
        {
            // No address at all is treated as local: the default is loopback,
            // and refusing to offer Start over an empty box would be obtuse.
            return true;
        }

        if (text.StartsWith('[') && text.EndsWith(']'))
        {
            text = text[1..^1];
        }

        foreach (var name in AlwaysLocal)
        {
            if (string.Equals(text, name, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        if (IPAddress.TryParse(text, out var parsed))
        {
            if (IPAddress.IsLoopback(parsed) || parsed.Equals(IPAddress.Any) || parsed.Equals(IPAddress.IPv6Any))
            {
                return true;
            }

            if (localAddresses is not null)
            {
                foreach (var candidate in localAddresses)
                {
                    if (IPAddress.TryParse(candidate?.Trim(), out var local) && local.Equals(parsed))
                    {
                        return true;
                    }
                }
            }

            return false;
        }

        // A host name. Compare the leading label as well as the whole string, so
        // "chaos-node" and "chaos-node.lan" both match a machine called
        // "CHAOS-NODE".
        if (string.IsNullOrWhiteSpace(machineName))
        {
            return false;
        }

        if (string.Equals(text, machineName, StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        var label = text.Split('.', 2)[0];
        return string.Equals(label, machineName, StringComparison.OrdinalIgnoreCase);
    }
}
