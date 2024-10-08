package api

import (
	"bytes"
	"net/http"
	"net/url"

	"github.com/canonical/lxd/lxd/response"
	"github.com/canonical/lxd/shared/api"
	"github.com/canonical/microcluster/v2/rest"
	"github.com/canonical/microcluster/v2/state"
	"github.com/gorilla/mux"

	"github.com/canonical/snap-openstack/sunbeam-microcluster/access"
	"github.com/canonical/snap-openstack/sunbeam-microcluster/sunbeam"
)

// /1.0/config/<name> endpoint.
var configCmd = rest.Endpoint{
	Path: "config/{key}",

	Get:    access.ClusterCATrustedEndpoint(cmdConfigGet, true),
	Put:    access.ClusterCATrustedEndpoint(cmdConfigPut, true),
	Delete: access.ClusterCATrustedEndpoint(cmdConfigDelete, true),
}

func cmdConfigGet(s state.State, r *http.Request) response.Response {
	var key string
	key, err := url.PathUnescape(mux.Vars(r)["key"])
	if err != nil {
		return response.InternalError(err)
	}
	config, err := sunbeam.GetConfig(r.Context(), s, key)
	if err != nil {
		if err, ok := err.(api.StatusError); ok {
			if err.Status() == http.StatusNotFound {
				return response.NotFound(err)
			}
		}
		return response.InternalError(err)
	}

	return response.SyncResponse(true, config)
}

func cmdConfigPut(s state.State, r *http.Request) response.Response {
	key, err := url.PathUnescape(mux.Vars(r)["key"])
	if err != nil {
		return response.InternalError(err)
	}

	var body bytes.Buffer
	_, err = body.ReadFrom(r.Body)
	if err != nil {
		return response.InternalError(err)
	}

	err = sunbeam.UpdateConfig(r.Context(), s, key, body.String())
	if err != nil {
		return response.InternalError(err)
	}

	return response.EmptySyncResponse
}

func cmdConfigDelete(s state.State, r *http.Request) response.Response {
	key, err := url.PathUnescape(mux.Vars(r)["key"])
	if err != nil {
		return response.InternalError(err)
	}

	err = sunbeam.DeleteConfig(r.Context(), s, key)
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
