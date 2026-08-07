namespace Chaos.Shell.Core;

/// <summary>Whether administrator rights stand between the operator and an action.</summary>
public enum ElevationNeed
{
    /// <summary>The action does not involve anything privileged.</summary>
    NotRequired = 0,

    /// <summary>
    /// It usually needs administrator rights, but this account may have been
    /// granted them for this specific service. Worth attempting.
    /// </summary>
    Advisory = 1,

    /// <summary>Established as refused. Attempting again unelevated is pointless.</summary>
    Required = 2,
}

/// <summary>
/// The elevation position for one action.
/// </summary>
/// <param name="Need">How hard the requirement is.</param>
/// <param name="Explanation">What to tell the operator before they press it.</param>
/// <param name="OfferRelaunch">
/// Whether to offer "restart this shell as administrator". Never offered when
/// the shell is already elevated: an operator who is already an administrator
/// and is still refused has a different problem, and being told to do the thing
/// they have already done is how a UI loses their trust.
/// </param>
public sealed record ElevationDecision(ElevationNeed Need, string Explanation, bool OfferRelaunch)
{
    public static readonly ElevationDecision NotNeeded =
        new(ElevationNeed.NotRequired, string.Empty, false);
}

/// <summary>
/// Decides when the shell has to ask Windows for more rights, and what to say
/// about it.
/// </summary>
/// <remarks>
/// The rule: never fail with an opaque error where an explanation and a way
/// through would do. An operator standing at a homestead node at -20 °C, being
/// told "the operation could not be completed" by the thing that controls their
/// freeze protection, is a failure of this UI and not of Windows.
/// </remarks>
public static class Elevation
{
    /// <summary>Wording for the relaunch button, in one place.</summary>
    public const string RelaunchLabel = "Restart this shell as administrator";

    /// <summary>
    /// Starting or stopping a Windows service. Advisory rather than required:
    /// service control rights can be granted to a non-administrator account,
    /// and refusing to try would break a correctly locked-down node.
    /// </summary>
    public static ElevationDecision ForServiceControl(bool isElevated, string verb, string serviceName)
    {
        if (isElevated)
        {
            return new ElevationDecision(
                ElevationNeed.NotRequired,
                $"This shell is running as administrator, so it can {verb} the service '{serviceName}'.",
                OfferRelaunch: false);
        }

        return new ElevationDecision(
            ElevationNeed.Advisory,
            $"Windows usually requires administrator rights to {verb} a service. This shell is not "
            + "running as one, so the attempt may be refused — if it is, the shell will offer to "
            + "restart itself with them.",
            OfferRelaunch: false);
    }

    /// <summary>After Windows has actually refused. Now it is established.</summary>
    public static ElevationDecision AfterAccessDenied(bool isElevated, string action)
    {
        if (isElevated)
        {
            return new ElevationDecision(
                ElevationNeed.Required,
                $"Windows refused to {action} even though this shell is already running as "
                + "administrator. This is a permission set on the service itself, not something "
                + "restarting the shell will fix.",
                OfferRelaunch: false);
        }

        return new ElevationDecision(
            ElevationNeed.Required,
            $"Windows refused to {action} because this shell does not have administrator rights.",
            OfferRelaunch: true);
    }

    /// <summary>
    /// Starting the platform as a child of this shell. Never privileged — and
    /// that is worth saying, because it is the way through when service control
    /// is refused and nobody with administrator rights is available.
    /// </summary>
    public static ElevationDecision ForManagedChild() => new(
        ElevationNeed.NotRequired,
        "Starting the platform under this shell does not need administrator rights.",
        OfferRelaunch: false);

    /// <summary>Installing a Windows service. Always privileged.</summary>
    public static ElevationDecision ForServiceInstall(bool isElevated) => isElevated
        ? new ElevationDecision(
            ElevationNeed.NotRequired,
            "This shell is running as administrator and can install a Windows service.",
            OfferRelaunch: false)
        : new ElevationDecision(
            ElevationNeed.Required,
            "Installing a Windows service always needs administrator rights.",
            OfferRelaunch: true);
}
