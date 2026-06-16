require "date"
require "sinatra"

set :bind, "0.0.0.0"
set :port, ENV.fetch("PORT", "8000").to_i
set :protection, host_authorization: { permitted_hosts: ["localhost", "127.0.0.1", ".localhost"] }

helpers do
  def h(value)
    Rack::Utils.escape_html(value.to_s)
  end
end

get "/" do
  erb :home
end

get "/about" do
  erb :about
end

get "/limits" do
  erb :limits
end

get "/workspace" do
  erb :workspace
end

post "/workspace/create" do
  title = h(params.fetch("title", "New calendar note"))
  owner = h(params.fetch("owner", "ops@example.test"))
  erb :action_result, locals: {
    action: "Created",
    summary: "Created #{title} for #{owner}.",
  }
end

post "/workspace/update" do
  entry_id = h(params.fetch("entry_id", "calendar-001"))
  status = h(params.fetch("status", "Active"))
  erb :action_result, locals: {
    action: "Updated",
    summary: "Updated #{entry_id} to #{status}.",
  }
end

post "/workspace/delete" do
  entry_id = h(params.fetch("entry_id", "calendar-001"))
  erb :action_result, locals: {
    action: "Deleted",
    summary: "Marked #{entry_id} for deletion review.",
  }
end

get "/calendar/:year/:month/:day" do
  current = Date.new(params[:year].to_i, params[:month].to_i, params[:day].to_i)
  next_day = current.next_day
  prev_day = current.prev_day
  erb :calendar, locals: { current: current, next_day: next_day, prev_day: prev_day }
end
