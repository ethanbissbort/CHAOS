namespace Chaos.Host.Routing;

/// <summary>
/// Thrown when the route-ownership manifest is malformed, ambiguous, or claims
/// a prefix for .NET that no .NET endpoint actually serves.
/// </summary>
/// <remarks>
/// This exception is always fatal to host startup, by design. A route marked
/// <see cref="RouteOwner.Dotnet"/> with nothing behind it returns 404 — which
/// during a migration is how a control system quietly loses an alarm endpoint.
/// Failing to start is loud; a 404 is not.
/// </remarks>
public sealed class RouteOwnershipException : Exception
{
    /// <summary>Creates the exception with a message.</summary>
    /// <param name="message">A message that names the offending prefixes and what to do about them.</param>
    public RouteOwnershipException(string message)
        : base(message)
    {
    }

    /// <summary>Creates the exception with a message and an inner cause.</summary>
    /// <param name="message">A message that names the offending prefixes and what to do about them.</param>
    /// <param name="innerException">The underlying cause.</param>
    public RouteOwnershipException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}
