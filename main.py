"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Navbar } from "@/components/navbar";
import { PROVIDERS } from "@/lib/providers";
import { ProviderCard } from "@/components/provider-card";
import { PendingRequests } from "@/components/pending-requests";
import { TeamMembers } from "@/components/team-members";
import { InviteShare } from "@/components/invite-share";
import { useGatewayKeys } from "@/lib/gateway-context";
import { supabase } from "@/lib/supabase-client";

const API_URL = process.env.NEXT_PUBLIC_API_URL;

export default function GatewayPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [portalLoading, setPortalLoading] = useState(false);
  const { role, status, subscriptionStatus, inviteCode, companyName, userId } = useGatewayKeys();

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (!session) {
        router.push("/sign-in");
      } else {
        setChecking(false);
      }
    });
  }, [router]);

  async function handleManageBilling() {
    setPortalLoading(true);
    const {
      data: { session },
    } = await supabase.auth.getSession();
    if (!session) return;

    const res = await fetch(`${API_URL}/v1/billing/portal`, {
      method: "POST",
      headers: { Authorization: `Bearer ${session.access_token}` },
    });

    if (res.ok) {
      const { url } = await res.json();
      window.location.href = url;
    } else {
      setPortalLoading(false);
    }
  }

  if (checking) {
    return (
      <>
        <Navbar />
        <section className="max-w-5xl mx-auto px-4 py-20 text-center text-muted">Loading...</section>
      </>
    );
  }

  if (status === "pending") {
    return (
      <>
        <Navbar />
        <section className="max-w-md mx-auto px-4 py-20 text-center">
          <h1 className="text-2xl font-semibold mb-3">Waiting for approval</h1>
          <p className="text-muted">
            Your request to join {companyName || "this company"} is pending. An admin needs to approve
            you before you can use the gateway.
          </p>
        </section>
      </>
    );
  }

  const needsBilling = role === "admin" && subscriptionStatus !== "trialing" && subscriptionStatus !== "active";

  return (
    <>
      <Navbar />
      <section className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-20">
        <div className="text-center mb-12">
          {companyName && (
            <p className="text-base text-muted mb-3">
              Signed in with Company <span className="text-foreground font-semibold">{companyName}</span>
            </p>
          )}
          <h1 className="text-3xl font-semibold tracking-tight mb-3">Connect a provider</h1>
          <p className="text-muted max-w-lg mx-auto">
            Add your API key for any provider below to start routing requests through NetCost.ai's gateway.
          </p>
        </div>

        {needsBilling && (
          <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 mb-10 text-center">
            <p className="text-sm text-red-400 mb-2">Billing setup isn't complete yet.</p>
            <a href="/billing/setup" className="text-sm font-medium bg-primary hover:bg-primary-hover text-white px-4 py-2 rounded-lg transition-colors inline-block">
              Complete billing setup
            </a>
          </div>
        )}

        {role === "admin" && (
          <>
            <div className="text-center mb-6 flex justify-center gap-4">
              <a href="/usage" className="text-sm text-primary hover:underline">
                View team usage →
              </a>
              {(subscriptionStatus === "trialing" || subscriptionStatus === "active") && (
                <button onClick={handleManageBilling} disabled={portalLoading} className="text-sm text-primary hover:underline disabled:opacity-50">
                  {portalLoading ? "Loading..." : "Manage billing →"}
                </button>
              )}
            </div>
            <TeamMembers currentUserId={userId} />
            <PendingRequests />
            {inviteCode && (
              <div className="rounded-xl border border-border bg-surface p-4 mb-10 text-center">
                <p className="text-sm text-muted mb-1">Invite teammates to {companyName} with this code:</p>
                <p className="font-mono text-lg tracking-wide">{inviteCode}</p>
                <InviteShare inviteCode={inviteCode} companyName={companyName || "our team"} />
              </div>
            )}
          </>
        )}

        <div className="grid sm:grid-cols-3 gap-6">
          {PROVIDERS.map((p) => (
            <ProviderCard key={p.id} id={p.id} displayName={p.displayName} color={p.color} devConsoleUrl={p.devConsoleUrl} />
          ))}
        </div>
      </section>
    </>
  );
}