package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/url"

	"github.com/canonical/lxd/lxd/response"
	"github.com/canonical/lxd/lxd/util"
	"github.com/canonical/lxd/shared/api"
	"github.com/canonical/microcluster/v2/rest"
	"github.com/canonical/microcluster/v2/state"
	"github.com/gorilla/mux"

	"github.com/canonical/snap-openstack/sunbeam-microcluster/access"
	"github.com/canonical/snap-openstack/sunbeam-microcluster/sunbeam"
)

// /1.0/terraformstate endpoint.
var terraformStateListCmd = rest.Endpoint{
	Path: "terraformstate",

	Get: access.ClusterCATrustedEndpoint(cmdStateList, false),
}

// /1.0/terraformstate/{name} endpoint.
// The endpoints are basically to provide REST URLs to Terraform http
// backend configuration to maintain Terraform state centrally with
// locking mechanism.
// Terraform 1.3.x doesnot support passing certs to the REST URL for
// authentications and so the endpoints are exposed as AllowUntrusted.
// TODO: Newer version yet to release 1.4.x supports TLS authentication
// to http backend. Once sunbeam moves to use 1.4.x, change the
// endpoints not to allow untrusted.
// https://github.com/hashicorp/terraform/commit/75e5ae27a258122fe6bf122beb943324c69de5b1
var terraformStateCmd = rest.Endpoint{
	Path: "terraformstate/{name}",

	Get:    access.ClusterCATrustedEndpoint(cmdStateGet, false),
	Put:    access.ClusterCATrustedEndpoint(cmdStatePut, false),
	Delete: access.ClusterCATrustedEndpoint(cmdStateDelete, false),
}

// /1.0/terraformlock endpoint.
var terraformLockListCmd = rest.Endpoint{
	Path: "terraformlock",

	Get: access.ClusterCATrustedEndpoint(cmdLockList, false),
}

// /1.0/terraformlock/{name} endpoint.
var terraformLockCmd = rest.Endpoint{
	Path: "terraformlock/{name}",

	Get: access.ClusterCATrustedEndpoint(cmdLockGet, false),
	Put: access.ClusterCATrustedEndpoint(cmdLockPut, false),
}

// /1.0/terraformunlock/{name} endpoint.
var terraformUnlockCmd = rest.Endpoint{
	Path: "terraformunlock/{name}",

	Put: access.ClusterCATrustedEndpoint(cmdUnlockPut, false),
}

func cmdStateList(s state.State, r *http.Request) response.Response {
	plans, err := sunbeam.GetTerraformStates(r.Context(), s)

	if err != nil {
		return response.InternalError(err)
	}

	return response.SyncResponse(true, plans)
}

func cmdStateGet(s state.State, r *http.Request) response.Response {
	var name string

	name, err := url.PathUnescape(mux.Vars(r)["name"])
	if err != nil {
		return response.InternalError(err)
	}

	state, err := sunbeam.GetTerraformState(r.Context(), s, name)
	if err != nil {
		if err, ok := err.(api.StatusError); ok {
			if err.Status() == http.StatusNotFound {
				return response.NotFound(err)
			}
		}
		return response.InternalError(err)
	}

	var jsonState map[string]interface{}
	err = json.Unmarshal([]byte(state), &jsonState)
	if err != nil {
		return response.InternalError(err)
	}

	// Just send state data instead of SyncResponse Json object as
	// terraform expects just state data.
	return response.ManualResponse(func(w http.ResponseWriter) error {
		return util.WriteJSON(w, jsonState, nil)
	})
}

func cmdStatePut(s state.State, r *http.Request) response.Response {
	var name string

	name, err := url.PathUnescape(mux.Vars(r)["name"])
	if err != nil {
		return response.InternalError(err)
	}

	lockID := r.URL.Query().Get("ID")

	var body bytes.Buffer
	_, err = body.ReadFrom(r.Body)
	if err != nil {
		return response.InternalError(err)
	}

	dbLock, err := sunbeam.UpdateTerraformState(r.Context(), s, name, lockID, body.String())
	if err != nil {
		if err, ok := err.(api.StatusError); ok {
			if err.Status() == http.StatusConflict {
				jsonDBLock, err := json.Marshal(dbLock)
				if err != nil {
					return response.InternalError(err)
				}

				return response.ManualResponse(func(w http.ResponseWriter) error {
					w.WriteHeader(http.StatusConflict)
					return util.WriteJSON(w, jsonDBLock, nil)
				})
			}
		}
		return response.InternalError(err)
	}

	return response.EmptySyncResponse
}

func cmdStateDelete(s state.State, r *http.Request) response.Response {
	var name string

	name, err := url.PathUnescape(mux.Vars(r)["name"])
	if err != nil {
		return response.InternalError(err)
	}

	err = sunbeam.DeleteTerraformState(r.Context(), s, name)
	if err != nil {
		if err, ok := err.(api.StatusError); ok {
			if err.Status() == http.StatusNotFound {
				return response.NotFound(err)
			}
		}
		return response.InternalError(err)
	}

	return response.EmptySyncResponse
}

func cmdLockList(s state.State, r *http.Request) response.Response {
	plans, err := sunbeam.GetTerraformLocks(r.Context(), s)

	if err != nil {
		return response.InternalError(err)
	}

	return response.SyncResponse(true, plans)
}

func cmdLockGet(s state.State, r *http.Request) response.Response {
	var name string

	name, err := url.PathUnescape(mux.Vars(r)["name"])
	if err != nil {
		return response.InternalError(err)
	}

	lock, err := sunbeam.GetTerraformLock(r.Context(), s, name)
	if err != nil {
		if err, ok := err.(api.StatusError); ok {
			if err.Status() == http.StatusNotFound {
				return response.NotFound(err)
			}
		}
		return response.InternalError(err)
	}

	// Just send state data instead of SyncResponse Json object as
	// terraform expects just state data.
	return response.ManualResponse(func(w http.ResponseWriter) error {
		return util.WriteJSON(w, lock, nil)
	})
}

func cmdLockPut(s state.State, r *http.Request) response.Response {
	var name string

	name, err := url.PathUnescape(mux.Vars(r)["name"])
	if err != nil {
		return response.InternalError(err)
	}

	var body bytes.Buffer
	_, err = body.ReadFrom(r.Body)
	if err != nil {
		return response.InternalError(err)
	}

	dbLock, err := sunbeam.UpdateTerraformLock(r.Context(), s, name, body.String())
	if err != nil {
		if err, ok := err.(api.StatusError); ok {
			jsonDBLock, err1 := json.Marshal(dbLock)
			if err1 != nil {
				return response.InternalError(err1)
			}
			if err.Status() == http.StatusLocked {
				return response.ManualResponse(func(w http.ResponseWriter) error {
					w.WriteHeader(http.StatusLocked)
					return util.WriteJSON(w, jsonDBLock, nil)
				})
			} else if err.Status() == http.StatusConflict {
				return response.ManualResponse(func(w http.ResponseWriter) error {
					w.WriteHeader(http.StatusConflict)
					return util.WriteJSON(w, jsonDBLock, nil)
				})
			}
		}
		return response.InternalError(err)
	}

	return response.EmptySyncResponse
}

func cmdUnlockPut(s state.State, r *http.Request) response.Response {
	var name string

	name, err := url.PathUnescape(mux.Vars(r)["name"])
	if err != nil {
		return response.InternalError(err)
	}

	var body bytes.Buffer
	_, err = body.ReadFrom(r.Body)
	if err != nil {
		return response.InternalError(err)
	}

	dbLock, err := sunbeam.DeleteTerraformLock(r.Context(), s, name, body.String())
	if err != nil {
		if err, ok := err.(api.StatusError); ok {
			jsonDBLock, err1 := json.Marshal(dbLock)
			if err1 != nil {
				return response.InternalError(err1)
			}
			if err.Status() == http.StatusConflict {
				return response.ManualResponse(func(w http.ResponseWriter) error {
					w.WriteHeader(http.StatusConflict)
					return util.WriteJSON(w, jsonDBLock, nil)
				})
			}
		}
		return response.InternalError(err)
	}

	return response.EmptySyncResponse
}
