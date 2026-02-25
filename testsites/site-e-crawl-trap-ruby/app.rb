require "date"
require "sinatra"

set :bind, "0.0.0.0"
set :port, ENV.fetch("PORT", "8000").to_i
set :protection, host_authorization: { permitted_hosts: ["localhost", "127.0.0.1", ".localhost"] }

get "/" do
  erb :home
end

get "/about" do
  erb :about
end

get "/limits" do
  erb :limits
end

get "/calendar/:year/:month/:day" do
  current = Date.new(params[:year].to_i, params[:month].to_i, params[:day].to_i)
  next_day = current.next_day
  prev_day = current.prev_day
  erb :calendar, locals: { current: current, next_day: next_day, prev_day: prev_day }
end
